"""project policy overrides

Revision ID: a72d6bf418e9
Revises: f1a83c6d2e47
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a72d6bf418e9"
down_revision = "f1a83c6d2e47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "sensitive_path_exceptions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "generated_path_exceptions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch_op.add_column(
            sa.Column("approval_gated_categories", sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "dependency_paths",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("approval_gated_categories")
        batch_op.drop_column("dependency_paths")
        batch_op.drop_column("generated_path_exceptions")
        batch_op.drop_column("sensitive_path_exceptions")
