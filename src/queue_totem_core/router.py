from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    STATUS_CHAMADO,
    STATUS_CONCLUIDO,
    STATUS_EM_ATENDIMENTO,
    STATUS_NA_FILA,
    STATUS_NAO_COMPARECEU,
    ALL_STATUSES,
    QueueTicket,
    utcnow,
)
from .priority import build_rank_map, pick_next, sort_queue
from .schemas import (
    DisplayOut,
    DisplayWithStationsOut,
    QueueConfig,
    StationDisplayOut,
    TicketCreate,
    TicketOut,
    TicketStationMoveOut,
    TicketStationUpdate,
    TicketStatusUpdate,
)

_SEQUENCE_MAX_ATTEMPTS = 3


async def _count_existing(
    db: AsyncSession, ticket_type: str, ticket_date: date, daily_reset: bool
) -> int:
    stmt = select(func.count()).select_from(QueueTicket).where(
        QueueTicket.ticket_type == ticket_type
    )
    if daily_reset:
        stmt = stmt.where(QueueTicket.ticket_date == ticket_date)
    return int(await db.scalar(stmt) or 0)


def build_queue_router(
    *,
    get_db: Callable[..., Any],
    config: QueueConfig,
    manage_dependency: Callable[..., Any] | None = None,
    read_dependency: Callable[..., Any] | None = None,
) -> APIRouter:
    """Monta o router da fila com a auth e a sessão de banco injetadas pelo host.

    - get_db: dependency do host que fornece uma AsyncSession (o pacote faz commit).
    - manage_dependency: protege chamar/gerenciar a fila. None = aberto (não recomendado).
    - read_dependency: protege a listagem da fila. None = aberto.
    - Emissão de senha (POST /tickets) e painel (GET /display) são sempre públicos:
      totem e TV são dispositivos físicos sem login.
    """
    router = APIRouter()
    rank_map = build_rank_map(config)
    types_by_code = {t.code: t for t in config.ticket_types}
    station_labels = config.station_labels
    multi_station = config.multi_station
    # Em modo estação única o painel devolve exatamente o JSON da v0.2.0 —
    # sem campo `stations` sequer presente.
    display_model = DisplayWithStationsOut if multi_station else DisplayOut
    tz = ZoneInfo(config.timezone) if config.timezone else None
    read_deps = [Depends(read_dependency)] if read_dependency else []
    manage_deps = [Depends(manage_dependency)] if manage_dependency else []

    def _today() -> date:
        return datetime.now(tz).date() if tz else date.today()

    def _to_out(ticket: QueueTicket, model: type[TicketOut] = TicketOut) -> Any:
        out = model.model_validate(ticket)
        ttype = types_by_code.get(ticket.ticket_type)
        out.ticket_label = ttype.label if ttype else None
        out.station_label = (
            station_labels.get(ticket.station) if ticket.station else None
        )
        return out

    def _require_multi_station() -> None:
        if not multi_station:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                "O pacote está em modo estação única: declare `stations` no "
                "QueueConfig para usar filas por estação",
            )

    def _validate_station(station: str) -> str:
        _require_multi_station()
        if station not in station_labels:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"Estação desconhecida: {station!r}",
            )
        return station

    async def _get_ticket(db: AsyncSession, ticket_id: int) -> QueueTicket:
        ticket = await db.get(QueueTicket, ticket_id)
        if ticket is None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Senha não encontrada")
        return ticket

    @router.post(
        "/tickets",
        response_model=TicketOut,
        status_code=http_status.HTTP_201_CREATED,
        summary="Emitir senha (totem — público)",
    )
    async def create_ticket(
        payload: TicketCreate, db: AsyncSession = Depends(get_db)
    ) -> TicketOut:
        ttype = types_by_code.get(payload.ticket_type)
        if ttype is None:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"Tipo de senha desconhecido: {payload.ticket_type!r}",
            )
        if ttype.priority_source == "none" and payload.is_priority:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"O tipo {ttype.code!r} não aceita prioridade",
            )

        today = _today()
        for _ in range(_SEQUENCE_MAX_ATTEMPTS):
            count = await _count_existing(db, ttype.code, today, config.daily_reset)
            sequence = count + 1
            now = utcnow()
            ticket = QueueTicket(
                ticket_type=ttype.code,
                ticket_date=today,
                sequence=sequence,
                ticket_number=f"{ttype.code}-{sequence:03d}",
                is_priority=payload.is_priority,
                priority_reason=payload.priority_reason if payload.is_priority else None,
                reference_code=payload.reference_code,
                reference_label=payload.reference_label,
                status=STATUS_NA_FILA,
                station=config.entry_station,
                station_entered_at=now if multi_station else None,
                queued_since=now,
            )
            db.add(ticket)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                continue
            await db.refresh(ticket)
            return _to_out(ticket)

        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            "Não foi possível gerar o número da senha (conflito de concorrência); tente novamente",
        )

    @router.get(
        "/display",
        response_model=display_model,
        summary="Painel de chamada (TV — público, polling)",
    )
    async def display(
        limit: int = Query(default=5, ge=1, le=20),
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        today = _today()
        current = await db.scalar(
            select(QueueTicket)
            .where(
                QueueTicket.ticket_date == today,
                QueueTicket.status == STATUS_CHAMADO,
            )
            .order_by(QueueTicket.called_at.desc())
            .limit(1)
        )
        recent = (
            await db.scalars(
                select(QueueTicket)
                .where(
                    QueueTicket.ticket_date == today,
                    QueueTicket.called_at.is_not(None),
                )
                .order_by(QueueTicket.called_at.desc())
                .limit(limit)
            )
        ).all()
        if not multi_station:
            return DisplayOut(
                current=_to_out(current) if current else None,
                recent=[_to_out(t) for t in recent],
            )

        stations: list[StationDisplayOut] = []
        for code, label in station_labels.items():
            station_current = await db.scalar(
                select(QueueTicket)
                .where(
                    QueueTicket.ticket_date == today,
                    QueueTicket.status == STATUS_CHAMADO,
                    QueueTicket.station == code,
                )
                .order_by(QueueTicket.called_at.desc())
                .limit(1)
            )
            stations.append(
                StationDisplayOut(
                    station=code,
                    station_label=label,
                    current=_to_out(station_current) if station_current else None,
                )
            )

        return DisplayWithStationsOut(
            current=_to_out(current) if current else None,
            recent=[_to_out(t) for t in recent],
            stations=stations,
        )

    @router.get(
        "/tickets",
        response_model=list[TicketOut],
        dependencies=read_deps,
        summary="Listar a fila do dia (prioridade + FIFO)",
    )
    async def list_tickets(
        status: str | None = Query(default=None),
        station: str | None = Query(
            default=None, description="Filtra a fila de uma estação (modo multi-estação)"
        ),
        db: AsyncSession = Depends(get_db),
    ) -> list[TicketOut]:
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST, f"Status desconhecido: {status!r}"
            )
        if station is not None:
            _validate_station(station)
        stmt = select(QueueTicket).where(QueueTicket.ticket_date == _today())
        if status is not None:
            stmt = stmt.where(QueueTicket.status == status)
        if station is not None:
            stmt = stmt.where(QueueTicket.station == station)
        tickets = list((await db.scalars(stmt)).all())
        return [_to_out(t) for t in sort_queue(tickets, rank_map)]

    @router.post(
        "/tickets/next",
        response_model=TicketOut,
        dependencies=manage_deps,
        summary="Chamar próxima senha",
    )
    async def call_next(
        station: str | None = Query(
            default=None,
            description=(
                "Chama a próxima da fila dessa estação. Sem o parâmetro, mantém o "
                "comportamento da v0.2.0: próxima senha global."
            ),
        ),
        db: AsyncSession = Depends(get_db),
    ) -> TicketOut:
        if station is not None:
            _validate_station(station)
        for _ in range(_SEQUENCE_MAX_ATTEMPTS):
            today = _today()
            waiting_stmt = select(QueueTicket).where(
                QueueTicket.ticket_date == today,
                QueueTicket.status == STATUS_NA_FILA,
            )
            if station is not None:
                waiting_stmt = waiting_stmt.where(QueueTicket.station == station)
            waiting = list((await db.scalars(waiting_stmt)).all())
            if not waiting:
                raise HTTPException(
                    http_status.HTTP_404_NOT_FOUND,
                    f"Não há senhas aguardando na estação {station!r}"
                    if station is not None
                    else "Não há senhas aguardando na fila",
                )

            called: list[QueueTicket] = []
            if config.normals_per_priority > 0:
                # A intercalação justa é contada dentro da própria estação: chamadas
                # de outra estação não podem consumir a cota de normais desta.
                called_stmt = (
                    select(QueueTicket)
                    .where(
                        QueueTicket.ticket_date == today,
                        QueueTicket.called_at.is_not(None),
                    )
                    .order_by(QueueTicket.called_at.asc(), QueueTicket.id.asc())
                )
                if station is not None:
                    called_stmt = called_stmt.where(QueueTicket.station == station)
                called = list((await db.scalars(called_stmt)).all())

            ticket = pick_next(waiting, called, rank_map, config.normals_per_priority)
            assert ticket is not None  # waiting não está vazio
            # Guarda otimista: se outro atendente chamou o mesmo ticket em paralelo,
            # o UPDATE condicional não afeta linha nenhuma e o loop tenta o seguinte.
            result = await db.execute(
                update(QueueTicket)
                .where(
                    QueueTicket.id == ticket.id,
                    QueueTicket.status == STATUS_NA_FILA,
                )
                .values(status=STATUS_CHAMADO, called_at=utcnow())
            )
            await db.commit()
            if result.rowcount == 1:
                await db.refresh(ticket)
                return _to_out(ticket)

        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            "Conflito de concorrência ao chamar a próxima senha; tente novamente",
        )

    @router.patch(
        "/tickets/{ticket_id}/recall",
        response_model=TicketOut,
        dependencies=manage_deps,
        summary="Chamar novamente",
    )
    async def recall(ticket_id: int, db: AsyncSession = Depends(get_db)) -> TicketOut:
        ticket = await _get_ticket(db, ticket_id)
        if ticket.status != STATUS_CHAMADO:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"Só é possível chamar novamente uma senha em 'chamado' (atual: {ticket.status!r})",
            )
        if ticket.recall_count >= config.max_recall_attempts:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                "Limite de rechamadas atingido; marque a senha como 'não compareceu'",
            )
        ticket.recall_count += 1
        ticket.called_at = utcnow()
        await db.commit()
        await db.refresh(ticket)
        return _to_out(ticket)

    @router.patch(
        "/tickets/{ticket_id}/no-show",
        response_model=TicketOut,
        dependencies=manage_deps,
        summary="Marcar não compareceu",
    )
    async def no_show(ticket_id: int, db: AsyncSession = Depends(get_db)) -> TicketOut:
        ticket = await _get_ticket(db, ticket_id)
        if ticket.status != STATUS_CHAMADO:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"Só é possível marcar não compareceu de uma senha em 'chamado' (atual: {ticket.status!r})",
            )
        ticket.status = STATUS_NAO_COMPARECEU
        ticket.finished_at = utcnow()
        await db.commit()
        await db.refresh(ticket)
        return _to_out(ticket)

    @router.patch(
        "/tickets/{ticket_id}/status",
        response_model=TicketOut,
        dependencies=manage_deps,
        summary="Transição manual: em_atendimento / concluido",
    )
    async def set_status(
        ticket_id: int, payload: TicketStatusUpdate, db: AsyncSession = Depends(get_db)
    ) -> TicketOut:
        ticket = await _get_ticket(db, ticket_id)
        allowed = {
            STATUS_EM_ATENDIMENTO: STATUS_CHAMADO,
            STATUS_CONCLUIDO: STATUS_EM_ATENDIMENTO,
        }
        if ticket.status != allowed[payload.status]:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST,
                f"Transição inválida: {ticket.status!r} -> {payload.status!r}",
            )
        ticket.status = payload.status
        if payload.status == STATUS_CONCLUIDO:
            ticket.finished_at = utcnow()
        await db.commit()
        await db.refresh(ticket)
        return _to_out(ticket)

    @router.patch(
        "/tickets/{ticket_id}/station",
        response_model=TicketStationMoveOut,
        dependencies=manage_deps,
        summary="Mover senha para outra estação (volta para a fila)",
    )
    async def move_station(
        ticket_id: int,
        payload: TicketStationUpdate,
        db: AsyncSession = Depends(get_db),
    ) -> TicketStationMoveOut:
        """Expressa "terminou aqui, foi para lá". Quem decide o destino é o host.

        O pacote não conhece a ordem entre estações e por isso não valida a
        transição: qualquer estação declarada é destino válido, a partir de
        qualquer status.
        """
        target = _validate_station(payload.station)
        ticket = await _get_ticket(db, ticket_id)

        now = utcnow()
        previous_station = ticket.station
        previous_seconds: float | None = None
        if ticket.station_entered_at is not None:
            entered = ticket.station_entered_at
            if entered.tzinfo is None:
                entered = entered.replace(tzinfo=timezone.utc)
            previous_seconds = (now - entered).total_seconds()

        ticket.station = target
        ticket.station_entered_at = now
        ticket.status = STATUS_NA_FILA
        # queued_since NÃO é tocado: é a chegada original e define o FIFO da
        # nova fila. Quem esperou na recepção não recomeça atrás de todo mundo.
        ticket.called_at = None
        ticket.finished_at = None
        ticket.recall_count = 0
        await db.commit()
        await db.refresh(ticket)

        out: TicketStationMoveOut = _to_out(ticket, TicketStationMoveOut)
        out.previous_station = previous_station
        out.previous_station_seconds = previous_seconds
        return out

    return router
