"""dependency bootstrap commands

Revision ID: f42b0c91a8d4
Revises: c1f4a7d29b60
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "f42b0c91a8d4"
down_revision = "c1f4a7d29b60"
branch_labels = None
depends_on = None

#: Deliberately NOT ``batch_alter_table``. On SQLite batch mode copies the
#: table into a new one and drops the original, and ``projects`` is the parent
#: of ``ON DELETE CASCADE`` children (tasks, and task_runs under them). With
#: ``PRAGMA foreign_keys=ON`` -- which the app's engine sets -- that drop
#: cascades, so a batch migration here silently empties a populated campaign
#: database. Adding and dropping a column needs no table rewrite on any
#: supported backend, so plain ALTER TABLE is both safe and sufficient.
_COLUMN = "dependency_bootstrap_commands"


def upgrade() -> None:
    if _COLUMN in _project_columns():
        return
    op.add_column(
        "projects",
        sa.Column(_COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    if _COLUMN not in _project_columns():
        return
    op.drop_column("projects", _COLUMN)


def _project_columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns("projects")}
