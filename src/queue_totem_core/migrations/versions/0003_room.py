"""sala da chamada: queue_tickets.room

Uma estação pode ter vários postos atendendo em paralelo (dois consultórios, dois
guichês). Sem a sala, o painel anuncia o nome e a pessoa não sabe para onde ir.

Nullable, como as demais: quem não usa postos paralelos simplesmente não envia o
parâmetro e o painel cai no rótulo da estação.

Revision ID: 0003_room
Revises: 0002_stations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_room"
down_revision = "0002_stations"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns("queue_tickets")}


def upgrade() -> None:
    if "room" not in _columns():
        op.add_column(
            "queue_tickets", sa.Column("room", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    if "room" in _columns():
        op.drop_column("queue_tickets", "room")
