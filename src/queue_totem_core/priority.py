from __future__ import annotations

from datetime import datetime

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


def arrival(ticket: QueueTicket) -> datetime:
    """Carimbo de chegada original — base do FIFO em qualquer estação.

    `queued_since` é preservado ao longo da jornada entre estações: quem esperou
    40 minutos na recepção não vai para o fim da fila do consultório. Cai para
    `created_at` em tickets anteriores à v0.3.0, onde a coluna é NULL.
    """
    return ticket.queued_since or ticket.created_at


def sort_queue(tickets: list[QueueTicket], rank_map: RankMap) -> list[QueueTicket]:
    """Prioridade via rank + FIFO (chegada original, id) dentro da mesma faixa."""
    return sorted(tickets, key=lambda t: (ticket_rank(t, rank_map), arrival(t), t.id))


def trailing_normal_streak(called: list[QueueTicket]) -> int:
    """Quantas senhas normais foram chamadas desde a última prioritária (ou o início do dia).

    `called` deve vir ordenado por called_at ascendente.
    """
    streak = 0
    for ticket in reversed(called):
        if ticket.is_priority:
            break
        streak += 1
    return streak


def pick_next(
    waiting: list[QueueTicket],
    called: list[QueueTicket],
    rank_map: RankMap,
    normals_per_priority: int = 0,
) -> QueueTicket | None:
    """Escolhe a próxima senha a chamar.

    - `waiting`: senhas em `na_fila`.
    - `called`: senhas já chamadas hoje, ordenadas por called_at ascendente
      (só é consultado quando `normals_per_priority > 0`).
    - `normals_per_priority == 0`: prioridade estrita — primeira da fila ordenada
      por `priority_order` + FIFO.
    - `normals_per_priority == N`: a cada N normais chamadas em sequência, a
      próxima é um prioritário aguardando (intercalação justa).
    """
    ordered = sort_queue(waiting, rank_map)
    if not ordered:
        return None
    if normals_per_priority <= 0:
        return ordered[0]

    priority = [t for t in ordered if t.is_priority]
    normal = [t for t in ordered if not t.is_priority]
    if not priority or not normal:
        return ordered[0]

    if trailing_normal_streak(called) >= normals_per_priority:
        return priority[0]
    return normal[0]
