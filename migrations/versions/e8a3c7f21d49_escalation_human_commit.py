"""escalation human commit (concern 73)

COMPLETED_BY_HAND marked a task COMPLETE without integrating any human-produced
commit. Downstream tasks started from a baseline that silently omitted the
human work. This column records the commit SHA an operator supplied when
resolving an escalation with COMPLETED_BY_HAND, so the orchestrator can
integrate it through the canonical mechanism and make dependency checks
truthful.

NULL means no commit was supplied -- the legitimate no-code completion path.

Revision ID: e8a3c7f21d49
Revises: b7c41d90e2a5
Create Date: 2026-09-29 12:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e8a3c7f21d49'
down_revision = 'a1f47b0c93d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('human_commit', sa.String(length=64)))


def downgrade() -> None:
    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.drop_column('human_commit')
