from .db import init_queue_tables
from .models import (
    ALL_STATUSES,
    STATUS_CHAMADO,
    STATUS_CONCLUIDO,
    STATUS_EM_ATENDIMENTO,
    STATUS_NA_FILA,
    STATUS_NAO_COMPARECEU,
    Base,
    QueueTicket,
)
from .router import build_queue_router
from .schemas import (
    DisplayOut,
    QueueConfig,
    TicketCreate,
    TicketOut,
    TicketStatusUpdate,
    TicketTypeConfig,
)

__version__ = "0.1.0"

__all__ = [
    "ALL_STATUSES",
    "STATUS_CHAMADO",
    "STATUS_CONCLUIDO",
    "STATUS_EM_ATENDIMENTO",
    "STATUS_NA_FILA",
    "STATUS_NAO_COMPARECEU",
    "Base",
    "DisplayOut",
    "QueueConfig",
    "QueueTicket",
    "TicketCreate",
    "TicketOut",
    "TicketStatusUpdate",
    "TicketTypeConfig",
    "build_queue_router",
    "init_queue_tables",
]
