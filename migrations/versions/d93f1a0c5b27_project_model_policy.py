"""project model policy (build.md section 31)

A project declares which registered model each role runs on: a default coder,
an optional stronger coder for HIGH-complexity tasks, and a reviewer. They
live on the project for the same reason the verification profile does -- they
are project-owned configuration declared in the manifest, not something a
model chooses at runtime.

Existing rows get ``{}``, which is the same as a manifest that declared no
``model_policy:`` block: no preference, so each role resolves to its first
enabled provider exactly as it did before this column existed.

Revision ID: d93f1a0c5b27
Revises: f42b0c91a8d4
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d93f1a0c5b27"
down_revision = "f42b0c91a8d4"
branch_labels = None
depends_on = None

#: Deliberately NOT ``batch_alter_table``, for the reason spelled out in
#: revision f42b0c91a8d4: on SQLite batch mode rewrites ``projects``, and the
#: drop of the original cascades to tasks and task_runs under
#: ``PRAGMA foreign_keys=ON``. Adding a column needs no rewrite.
_COLUMN = "model_policy"


def upgrade() -> None:
    if _COLUMN in _project_columns():
        return
    op.add_column(
        "projects",
        sa.Column(_COLUMN, sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    if _COLUMN not in _project_columns():
        return
    op.drop_column("projects", _COLUMN)


def _project_columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns("projects")}
