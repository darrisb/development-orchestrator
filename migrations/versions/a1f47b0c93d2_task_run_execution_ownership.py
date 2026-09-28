"""durable execution ownership for a task run

Revision ID: a1f47b0c93d2
Revises: e2d6b79a4f10
Create Date: 2026-09-28 14:00:00.000000

Concern 67. Existing rows start at generation 0 with no owner. That is the
truthful reading of a run written before this column existed: nothing holds it,
and the first dispatch that acquires it takes generation 1. It is deliberately
not a claim that the run's original executor is gone -- the generation fences
that executor whether or not it is, which is the whole point.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1f47b0c93d2"
down_revision = "e2d6b79a4f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "execution_generation",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("execution_owner", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "execution_started_at", sa.DateTime(timezone=True), nullable=True
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_column("execution_started_at")
        batch_op.drop_column("execution_owner")
        batch_op.drop_column("execution_generation")
