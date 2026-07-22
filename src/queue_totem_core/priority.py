from __future__ import annotations

from .models import QueueTicket
from .schemas import QueueConfig

RankMap = dict[tuple[str, bool], int]


def build_rank_map(config: QueueConfig) -> RankMap:
    """Mapeia (ticket_type, is_priority) -> rank (menor = atendido antes)."""
    return {combo: rank for rank, combo in enumerate(config.priority_order)}


def ticket_rank(ticket: QueueTicket, rank_map: RankMap) -> int:
    # Combinações fora do rank_map (config alterada com tickets antigos no banco)
    # vão para o fim da fila em vez de quebrar.
    return rank_map.get((ticket.ticket_type, ticket.is_priority), len(rank_map))


def sort_queue(tickets: list[QueueTicket], rank_map: RankMap) -> list[QueueTicket]:
    """Prioridade via rank + FIFO (created_at, id) dentro da mesma faixa."""
    return sorted(tickets, key=lambda t: (ticket_rank(t, rank_map), t.created_at, t.id))
