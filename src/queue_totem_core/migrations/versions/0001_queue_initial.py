"""Schema base da fila (equivalente ao create_all da v0.2.0).

Idempotente de propósito: bancos que já rodavam o pacote têm `queue_tickets`
criada por `init_queue_tables` e nenhuma linha de controle de versão. Nesses,
esta revisão não cria nada — só marca o ponto de partida em
`queue_versions_table`, e o delta real vem nas revisões seguintes.

Revision ID: 0001_queue_initial
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_queue_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("queue_tickets"):
        # Brownfield: tabela pré-existente criada por create_all. Nada a fazer
        # além do stamp que o próprio Alembic grava ao concluir a revisão.
        return

    op.create_table(
        "queue_tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_type", sa.String(length=32), nullable=False),
        sa.Column("ticket_date", sa.Date(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("ticket_number", sa.String(length=64), nullable=False),
        sa.Column("is_priority", sa.Boolean(), nullable=False),
        sa.Column("priority_reason", sa.String(length=255), nullable=True),
        sa.Column("reference_code", sa.String(length=255), nullable=True),
        sa.Column("reference_label", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("recall_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticket_type",
            "ticket_date",
            "sequence",
            name="uq_queue_tickets_type_date_sequence",
        ),
    )
    op.create_index(
        "ix_queue_tickets_ticket_type", "queue_tickets", ["ticket_type"], unique=False
    )
    op.create_index(
        "ix_queue_tickets_ticket_date", "queue_tickets", ["ticket_date"], unique=False
    )
    op.create_index(
        "ix_queue_tickets_status", "queue_tickets", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_queue_tickets_status", table_name="queue_tickets")
    op.drop_index("ix_queue_tickets_ticket_date", table_name="queue_tickets")
    op.drop_index("ix_queue_tickets_ticket_type", table_name="queue_tickets")
    op.drop_table("queue_tickets")
