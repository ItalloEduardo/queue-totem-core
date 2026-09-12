from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from .models import Base


async def init_queue_tables(engine: AsyncEngine) -> None:
    """Cria a(s) tabela(s) do pacote a partir do metadata atual.

    Idempotente (usa CREATE TABLE IF NOT EXISTS via checkfirst padrão) e
    deliberadamente fora da cadeia Alembic do host.

    Serve para **bancos novos e desenvolvimento**. Não é mecanismo de evolução:
    `create_all` não altera tabela existente, então uma coluna nova não aparece
    sozinha num banco que já rodava uma versão anterior. Para evoluir schema em
    produção, use a cadeia própria do pacote:

        python -m queue_totem_core.migrate upgrade head --url <URL>
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
