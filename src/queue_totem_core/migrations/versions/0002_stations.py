"""Filas por estação: station, station_entered_at, queued_since.

Todas nullable — nenhum projeto em modo estação única precisa preenchê-las, e
`queued_since` NULL cai para `created_at` na ordenação (ver priority.arrival).

Revision ID: 0002_stations
Revises: 0001_queue_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_stations"
down_revision = "0001_queue_initial"
branch_labels = None
depends_on = None


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns("queue_tickets")}


def upgrade() -> None:
    existing = _existing_columns()

    if "station" not in existing:
        op.add_column(
            "queue_tickets", sa.Column("station", sa.String(length=32), nullable=True)
        )
        op.create_index(
            "ix_queue_tickets_station", "queue_tickets", ["station"], unique=False
        )
    if "station_entered_at" not in existing:
        op.add_column(
            "queue_tickets",
            sa.Column("station_entered_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "queued_since" not in existing:
        op.add_column(
            "queue_tickets",
            sa.Column("queued_since", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    existing = _existing_columns()
    if "queued_since" in existing:
        op.drop_column("queue_tickets", "queued_since")
    if "station_entered_at" in existing:
        op.drop_column("queue_tickets", "station_entered_at")
    if "station" in existing:
        op.drop_index("ix_queue_tickets_station", table_name="queue_tickets")
        op.drop_column("queue_tickets", "station")
