"""escalation integration resolution commit (concern 73 follow-up)

Concern 73 records one SHA per COMPLETED_BY_HAND escalation and requires that
SHA to become an ancestor of ``agent/integration``. It assumed the human commit
would merge cleanly into the baseline. The recovered TraceStack history shows
the case that assumption misses: the human commit ``cbff2c4`` is a *sibling* of
the integration lineage ``fc6abc5``, both descending from the pre-TS-101 import
``06a0697``, and both appending to the same class and the same test file. The
canonical merge raises genuine content conflicts and the contract fails closed
-- correctly, and with Git fully intact.

The only way to get that work into the baseline is for a person to resolve the
conflict, which necessarily produces a **new** commit. The one existing column
cannot hold both facts: store the resolution there and the historical human
commit is lost from the record; store the human commit there and there is
nowhere to say which commit the baseline actually advanced onto.

This column is that second fact. It is NULL on every row written before this
revision and on every escalation resolved by a clean merge, so the meaning is
unambiguous: a non-NULL value means "the human source commit could not be
merged, and this is the distinct commit an operator had created to carry it
forward."

The pair is never optional together. The service layer refuses any state where
one is set and the other is not, so a partially-recorded reconciliation is a
visible error rather than a silent one.

Revision ID: c1f4a7d29b60
Revises: e8a3c7f21d49
Create Date: 2026-09-29 20:15:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c1f4a7d29b60'
down_revision = 'e8a3c7f21d49'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('integration_resolution_commit', sa.String(length=64))
        )


def downgrade() -> None:
    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.drop_column('integration_resolution_commit')
