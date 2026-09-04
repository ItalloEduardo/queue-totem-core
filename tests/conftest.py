from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from queue_totem_core import (
    QueueConfig,
    TicketTypeConfig,
    build_queue_router,
    init_queue_tables,
)

MANAGE_HEADERS = {"x-role": "manager"}
READ_HEADERS = {"x-role": "reader"}


def make_config(**overrides) -> QueueConfig:
    defaults = dict(
        ticket_types=[
            TicketTypeConfig(code="AG", label="Agendamento", priority_source="external"),
            TicketTypeConfig(code="TR", label="Triagem", priority_source="self_declared"),
            TicketTypeConfig(code="FM", label="Farmácia", priority_source="none"),
        ],
        priority_order=[
            ("AG", True),
            ("AG", False),
            ("TR", True),
            ("TR", False),
            ("FM", False),
        ],
        daily_reset=True,
        max_recall_attempts=1,
    )
    defaults.update(overrides)
    return QueueConfig(**defaults)


@pytest.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await init_queue_tables(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def config() -> QueueConfig:
    return make_config()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def make_app(session_factory, config: QueueConfig) -> FastAPI:
    async def get_db():
        async with session_factory() as session:
            yield session

    async def manage_dependency(x_role: str | None = Header(default=None)):
        if x_role != "manager":
            raise HTTPException(403, "Sem permissão de gerenciamento")

    async def read_dependency(x_role: str | None = Header(default=None)):
        if x_role is None:
            raise HTTPException(401, "Não autenticado")

    application = FastAPI()
    application.include_router(
        build_queue_router(
            get_db=get_db,
            config=config,
            manage_dependency=manage_dependency,
            read_dependency=read_dependency,
        ),
        prefix="/queue",
    )
    return application


@pytest.fixture
def app(config, session_factory) -> FastAPI:
    return make_app(session_factory, config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@asynccontextmanager
async def client_for(session_factory, config: QueueConfig):
    """Cliente HTTP para uma app com config sob medida (fora do fixture padrão)."""
    transport = ASGITransport(app=make_app(session_factory, config))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def emit(client, ticket_type: str, **kwargs) -> dict:
    resp = await client.post("/queue/tickets", json={"ticket_type": ticket_type, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def call_next(client) -> dict:
    resp = await client.post("/queue/tickets/next", headers=MANAGE_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()
