"""Ambiente Alembic do próprio pacote.

Roda numa cadeia separada da do host: a tabela de versões é
`queue_versions_table`, nunca `alembic_version`. Duas cadeias apontando para a
mesma tabela destroem o histórico uma da outra.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from queue_totem_core.models import Base

VERSION_TABLE = "queue_versions_table"

config = context.config
target_metadata = Base.metadata
PACKAGE_TABLES = set(target_metadata.tables)


def _get_url() -> str:
    url = (
        config.attributes.get("url")
        or config.get_main_option("sqlalchemy.url", None)
        or os.getenv("QUEUE_TOTEM_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "URL do banco não informada. Use --url, ou exporte "
            "QUEUE_TOTEM_DATABASE_URL / DATABASE_URL."
        )
    return str(url)


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Blinda o host: autogenerate só enxerga as tabelas do pacote."""
    if type_ == "table":
        return name in PACKAGE_TABLES
    return True


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=_include_object,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=_include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(section: dict) -> None:
    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    url = _get_url()
    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = url

    if make_url(url).get_dialect().is_async:
        asyncio.run(_run_async(section))
        return

    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        _do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
