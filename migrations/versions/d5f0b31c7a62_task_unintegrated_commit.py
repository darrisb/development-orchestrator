"""task unintegrated commit (concern 51)

A task could be COMPLETE with a reviewed candidate whose work was *not* in the
cumulative integration baseline: the merge conflicted, or the merged tree failed
the project's own verification. Nothing recorded that, so dependency scheduling
read COMPLETE as "its output is available" and a dependent task was started
against a baseline without it.

This column is the record. NULL -- the value every existing row gets -- means
nothing of the task is outstanding, which is true of every task delivered before
this revision: either it integrated, or its project had no baseline yet.

Revision ID: d5f0b31c7a62
Revises: a72d6bf418e9
Create Date: 2026-09-27 10:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd5f0b31c7a62'
down_revision = 'a72d6bf418e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unintegrated_commit', sa.String(length=64)))


def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('unintegrated_commit')
