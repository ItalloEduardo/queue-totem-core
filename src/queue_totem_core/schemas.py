from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import STATUS_CONCLUIDO, STATUS_EM_ATENDIMENTO

PrioritySource = Literal["external", "self_declared", "none"]


class TicketTypeConfig(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1)
    priority_source: PrioritySource
    # "external"      -> prioridade vem de fora (o host informa is_priority ao emitir)
    # "self_declared" -> quem emite (o totem) declara a prioridade na emissão
    # "none"          -> tipo nunca é prioritário


class QueueConfig(BaseModel):
    ticket_types: list[TicketTypeConfig] = Field(min_length=1)
    priority_order: list[tuple[str, bool]] = Field(min_length=1)
    daily_reset: bool = True
    max_recall_attempts: int = Field(default=1, ge=0)
    timezone: str | None = None
    # timezone IANA (ex.: "America/Fortaleza") usada para definir o "dia" da fila.
    # None -> data local do servidor. Importante em containers rodando em UTC.
    normals_per_priority: int = Field(default=0, ge=0)
    # Política de "chamar próximo" quando há prioritários e normais aguardando:
    #   0  -> prioridade estrita: esvazia todos os prioritários antes dos normais,
    #         seguindo priority_order + FIFO (comportamento padrão).
    #   N>0 -> intercalação justa: a cada N senhas normais chamadas, a próxima
    #         chamada é de um prioritário aguardando. Evita que uma enxurrada de
    #         prioritários trave indefinidamente a fila normal.
    # Dentro de cada faixa (prioritário / normal) a ordem continua sendo
    # priority_order + FIFO.

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except Exception as exc:
                raise ValueError(f"timezone inválida: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _validate_consistency(self) -> "QueueConfig":
        codes = [t.code for t in self.ticket_types]
        if len(codes) != len(set(codes)):
            raise ValueError("ticket_types contém códigos duplicados")

        types_by_code = {t.code: t for t in self.ticket_types}
        seen: set[tuple[str, bool]] = set()
        for code, is_priority in self.priority_order:
            ttype = types_by_code.get(code)
            if ttype is None:
                raise ValueError(f"priority_order referencia tipo desconhecido: {code!r}")
            if ttype.priority_source == "none" and is_priority:
                raise ValueError(
                    f"priority_order contém ({code!r}, True), mas esse tipo tem "
                    "priority_source='none' e nunca é prioritário"
                )
            if (code, is_priority) in seen:
                raise ValueError(f"priority_order contém entrada duplicada: ({code!r}, {is_priority})")
            seen.add((code, is_priority))

        for ttype in self.ticket_types:
            required = (
                [(ttype.code, False)]
                if ttype.priority_source == "none"
                else [(ttype.code, True), (ttype.code, False)]
            )
            for combo in required:
                if combo not in seen:
                    raise ValueError(
                        f"priority_order não cobre a combinação {combo} — toda combinação "
                        "possível de (tipo, prioridade) precisa ter um rank definido"
                    )
        return self


class TicketCreate(BaseModel):
    ticket_type: str
    reference_code: str | None = Field(default=None, max_length=255)
    reference_label: str | None = Field(default=None, max_length=255)
    is_priority: bool = False
    priority_reason: str | None = Field(default=None, max_length=255)


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_type: str
    ticket_label: str | None = None
    ticket_date: date
    sequence: int
    ticket_number: str
    is_priority: bool
    priority_reason: str | None
    reference_code: str | None
    reference_label: str | None
    status: str
    recall_count: int
    created_at: datetime
    called_at: datetime | None
    finished_at: datetime | None


class TicketStatusUpdate(BaseModel):
    status: Literal[STATUS_EM_ATENDIMENTO, STATUS_CONCLUIDO]  # type: ignore[valid-type]


class DisplayOut(BaseModel):
    current: TicketOut | None
    recent: list[TicketOut]
