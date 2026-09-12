from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Header, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from queue_totem_core import (
    QueueConfig,
    StationConfig,
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


STATIONS = [
    StationConfig(code="recepcao", label="Recepção"),
    StationConfig(code="consultorio", label="Consultório"),
    StationConfig(code="farmacia", label="Farmácia"),
]


def make_station_config(**overrides) -> QueueConfig:
    """Config em modo multi-estação, com a recepção como porta de entrada."""
    base = dict(stations=STATIONS, entry_station="recepcao")
    base.update(overrides)
    return make_config(**base)


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


async def call_next(client, station: str | None = None, room: str | None = None) -> dict:
    params = {}
    if station is not None:
        params["station"] = station
    if room is not None:
        params["room"] = room
    resp = await client.post(
        "/queue/tickets/next", params=params or None, headers=MANAGE_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def move_station(client, ticket_id: int, station: str) -> dict:
    resp = await client.patch(
        f"/queue/tickets/{ticket_id}/station",
        json={"station": station},
        headers=MANAGE_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def station_config() -> QueueConfig:
    return make_station_config()


@pytest.fixture
async def station_client(session_factory, station_config):
    async with client_for(session_factory, station_config) as c:
        yield c
