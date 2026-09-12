"""A cadeia Alembic do pacote, nos dois caminhos que importam.

Banco vazio (greenfield) e banco que já rodava o pacote via `create_all` e não
tem controle de versão nenhum (brownfield). O segundo é o caso real de todo
projeto que sobe da v0.2.0 para a v0.3.0.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command

from queue_totem_core.migrate import VERSION_TABLE, build_config

# Schema exatamente como a v0.2.0 o criava, sem as colunas de estação.
V2_SCHEMA = """
CREATE TABLE queue_tickets (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    ticket_type VARCHAR(32) NOT NULL,
    ticket_date DATE NOT NULL,
    sequence INTEGER NOT NULL,
    ticket_number VARCHAR(64) NOT NULL,
    is_priority BOOLEAN NOT NULL,
    priority_reason VARCHAR(255),
    reference_code VARCHAR(255),
    reference_label VARCHAR(255),
    status VARCHAR(32) NOT NULL,
    recall_count INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    called_at DATETIME,
    finished_at DATETIME,
    CONSTRAINT uq_queue_tickets_type_date_sequence
        UNIQUE (ticket_type, ticket_date, sequence)
)
"""

STATION_COLUMNS = {"station", "station_entered_at", "queued_since"}
ROOM_COLUMN = "room"
HEAD_REVISION = "0003_room"


def _url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'queue.db'}"


def _columns(url: str, table: str = "queue_tickets") -> set[str]:
    engine = sa.create_engine(url)
    try:
        return {col["name"] for col in sa.inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _scalar(url: str, statement: str):
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text(statement)).scalar()
    finally:
        engine.dispose()


def _execute(url: str, *statements: str) -> None:
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sa.text(statement))
    finally:
        engine.dispose()


class TestGreenfield:
    def test_upgrade_creates_everything_from_scratch(self, tmp_path):
        url = _url(tmp_path)
        command.upgrade(build_config(url), "head")

        assert "queue_tickets" in _tables(url)
        assert STATION_COLUMNS <= _columns(url)
        assert ROOM_COLUMN in _columns(url)

    def test_version_is_recorded_in_the_package_table(self, tmp_path):
        url = _url(tmp_path)
        command.upgrade(build_config(url), "head")

        assert VERSION_TABLE == "queue_versions_table"
        assert VERSION_TABLE in _tables(url)
        assert _scalar(url, f"SELECT version_num FROM {VERSION_TABLE}") == HEAD_REVISION

    def test_upgrade_is_repeatable(self, tmp_path):
        url = _url(tmp_path)
        command.upgrade(build_config(url), "head")
        command.upgrade(build_config(url), "head")
        assert STATION_COLUMNS <= _columns(url)


class TestBrownfield:
    def test_stamps_existing_table_and_applies_only_the_delta(self, tmp_path):
        url = _url(tmp_path)
        # Banco como está hoje em produção: tabela criada por create_all, com
        # dados, e nenhuma linha de controle de versão.
        _execute(
            url,
            V2_SCHEMA,
            """
            INSERT INTO queue_tickets (
                ticket_type, ticket_date, sequence, ticket_number, is_priority,
                status, recall_count, created_at
            ) VALUES ('AG', '2026-09-11', 1, 'AG-001', 0, 'na_fila', 0, '2026-09-11 08:00:00')
            """,
        )
        assert VERSION_TABLE not in _tables(url)

        command.upgrade(build_config(url), "head")

        # A tabela não foi recriada: o dado continua lá.
        assert _scalar(url, "SELECT COUNT(*) FROM queue_tickets") == 1
        assert _scalar(url, "SELECT ticket_number FROM queue_tickets") == "AG-001"
        # E os deltas posteriores foram aplicados.
        assert STATION_COLUMNS <= _columns(url)
        assert ROOM_COLUMN in _columns(url)
        assert _scalar(url, f"SELECT version_num FROM {VERSION_TABLE}") == HEAD_REVISION

    def test_rows_predating_v030_have_null_stations(self, tmp_path):
        url = _url(tmp_path)
        _execute(
            url,
            V2_SCHEMA,
            """
            INSERT INTO queue_tickets (
                ticket_type, ticket_date, sequence, ticket_number, is_priority,
                status, recall_count, created_at
            ) VALUES ('AG', '2026-09-11', 1, 'AG-001', 0, 'na_fila', 0, '2026-09-11 08:00:00')
            """,
        )
        command.upgrade(build_config(url), "head")
        assert _scalar(url, "SELECT station FROM queue_tickets") is None
        assert _scalar(url, "SELECT queued_since FROM queue_tickets") is None


class TestHostIsolation:
    def test_does_not_touch_the_host_alembic_version(self, tmp_path):
        url = _url(tmp_path)
        # Cadeia do host, no mesmo banco.
        _execute(
            url,
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
            "INSERT INTO alembic_version (version_num) VALUES ('host_head')",
        )

        command.upgrade(build_config(url), "head")

        assert _scalar(url, "SELECT version_num FROM alembic_version") == "host_head"
        assert _scalar(url, f"SELECT version_num FROM {VERSION_TABLE}") == HEAD_REVISION


class TestDowngrade:
    def test_downgrade_removes_the_station_columns(self, tmp_path):
        url = _url(tmp_path)
        command.upgrade(build_config(url), "head")
        command.downgrade(build_config(url), "0001_queue_initial")
        assert not (STATION_COLUMNS & _columns(url))
