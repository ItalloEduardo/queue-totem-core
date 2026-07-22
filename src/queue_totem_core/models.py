from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUS_NA_FILA = "na_fila"
STATUS_CHAMADO = "chamado"
STATUS_EM_ATENDIMENTO = "em_atendimento"
STATUS_CONCLUIDO = "concluido"
STATUS_NAO_COMPARECEU = "nao_compareceu"

ALL_STATUSES = (
    STATUS_NA_FILA,
    STATUS_CHAMADO,
    STATUS_EM_ATENDIMENTO,
    STATUS_CONCLUIDO,
    STATUS_NAO_COMPARECEU,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Metadata próprio do pacote — não compartilhar com o Base do host."""


class QueueTicket(Base):
    __tablename__ = "queue_tickets"
    __table_args__ = (
        UniqueConstraint(
            "ticket_type",
            "ticket_date",
            "sequence",
            name="uq_queue_tickets_type_date_sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ticket_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_number: Mapped[str] = mapped_column(String(64), nullable=False)
    is_priority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority_reason: Mapped[str | None] = mapped_column(String(255))
    reference_code: Mapped[str | None] = mapped_column(String(255))
    reference_label: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=STATUS_NA_FILA, index=True
    )
    recall_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
