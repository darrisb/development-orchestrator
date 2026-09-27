"""durable task-run active runtime accounting

Revision ID: e2d6b79a4f10
Revises: b7c41d90e2a5
Create Date: 2026-09-27 18:00:00.000000

Historical rows receive zero accumulated active time and no open interval.
That is deliberately conservative: their wall-clock lifetime cannot be
converted into active execution with precision that was never recorded.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2d6b79a4f10"
down_revision = "b7c41d90e2a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("active_runtime_ms", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("active_started_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("task_runs", schema=None) as batch_op:
        batch_op.drop_column("active_started_at")
        batch_op.drop_column("active_runtime_ms")
