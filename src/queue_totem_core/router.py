from __future__ import annotations

from datetime import date, datetime
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
    QueueConfig,
    TicketCreate,
    TicketOut,
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
    tz = ZoneInfo(config.timezone) if config.timezone else None
    read_deps = [Depends(read_dependency)] if read_dependency else []
    manage_deps = [Depends(manage_dependency)] if manage_dependency else []

    def _today() -> date:
        return datetime.now(tz).date() if tz else date.today()

    def _to_out(ticket: QueueTicket) -> TicketOut:
        out = TicketOut.model_validate(ticket)
        ttype = types_by_code.get(ticket.ticket_type)
        out.ticket_label = ttype.label if ttype else None
        return out

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
        response_model=DisplayOut,
        summary="Painel de chamada (TV — público, polling)",
    )
    async def display(
        limit: int = Query(default=5, ge=1, le=20),
        db: AsyncSession = Depends(get_db),
    ) -> DisplayOut:
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
        return DisplayOut(
            current=_to_out(current) if current else None,
            recent=[_to_out(t) for t in recent],
        )

    @router.get(
        "/tickets",
        response_model=list[TicketOut],
        dependencies=read_deps,
        summary="Listar a fila do dia (prioridade + FIFO)",
    )
    async def list_tickets(
        status: str | None = Query(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> list[TicketOut]:
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(
                http_status.HTTP_400_BAD_REQUEST, f"Status desconhecido: {status!r}"
            )
        stmt = select(QueueTicket).where(QueueTicket.ticket_date == _today())
        if status is not None:
            stmt = stmt.where(QueueTicket.status == status)
        tickets = list((await db.scalars(stmt)).all())
        return [_to_out(t) for t in sort_queue(tickets, rank_map)]

    @router.post(
        "/tickets/next",
        response_model=TicketOut,
        dependencies=manage_deps,
        summary="Chamar próxima senha",
    )
    async def call_next(db: AsyncSession = Depends(get_db)) -> TicketOut:
        for _ in range(_SEQUENCE_MAX_ATTEMPTS):
            today = _today()
            waiting = list(
                (
                    await db.scalars(
                        select(QueueTicket).where(
                            QueueTicket.ticket_date == today,
                            QueueTicket.status == STATUS_NA_FILA,
                        )
                    )
                ).all()
            )
            if not waiting:
                raise HTTPException(
                    http_status.HTTP_404_NOT_FOUND, "Não há senhas aguardando na fila"
                )

            called: list[QueueTicket] = []
            if config.normals_per_priority > 0:
                called = list(
                    (
                        await db.scalars(
                            select(QueueTicket)
                            .where(
                                QueueTicket.ticket_date == today,
                                QueueTicket.called_at.is_not(None),
                            )
                            .order_by(
                                QueueTicket.called_at.asc(), QueueTicket.id.asc()
                            )
                        )
                    ).all()
                )

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

    return router
