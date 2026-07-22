from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from .models import Base


async def init_queue_tables(engine: AsyncEngine) -> None:
    """Cria a(s) tabela(s) do pacote. Chamar na inicialização do app host.

    Idempotente (usa CREATE TABLE IF NOT EXISTS via checkfirst padrão).
    Deliberadamente fora de qualquer cadeia Alembic do host.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
