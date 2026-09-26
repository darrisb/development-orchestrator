"""task declared files (build.md section 6)

A task declares what the coder should read and what it may write. The context
builder loads the declared files first (section 15 priority 2) and the scope
guard later measures the diff against the writable ones (section 20).

Existing rows get an empty list, which means "the task declared no boundary":
the same as every task imported before this revision.

Revision ID: 8f2a17b4c903
Revises: 3c61aedc90c5
Create Date: 2026-09-26 12:10:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '8f2a17b4c903'
down_revision = '3c61aedc90c5'
branch_labels = None
depends_on = None

_COLUMNS = ('files_to_inspect', 'files_to_modify', 'files_to_create')


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        for name in _COLUMNS:
            batch_op.add_column(
                sa.Column(name, sa.JSON(), nullable=False, server_default='[]')
            )


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        for name in reversed(_COLUMNS):
            batch_op.drop_column(name)
