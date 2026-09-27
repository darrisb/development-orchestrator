"""model call audit fields (crash/resume durability)

A model call that failed or timed out left no ``model_runs`` row at all. The
row was written inside the transaction of the turn that raised, so the rollback
that unwound the turn also unwound the record of the call that caused it -- and
a table holding only the calls that worked cannot answer how often an endpoint
times out, which is the question sections 34 and 35 are arithmetic over.

The row that did survive, before the rollback, could not answer it either: it
carried ``duration_ms = 0`` because nothing measured the wait, so a 600-second
timeout was indistinguishable from a call that never left.

Three columns, all nullable, so every existing row keeps its current meaning:

* ``error_detail`` -- the failure, sanitized and bounded. An exception's own
  ``str()`` can contain a URL with a key in it, so it goes through the same
  redactor as a log.
* ``attempt`` -- which coding attempt the call belonged to, so a run's calls
  can be grouped the way ``attempt_number`` groups its work.
* ``review_cycle`` -- which review cycle, so a correction is distinguishable
  from a first answer in the table and not only in the artifacts.

NULL on all three is the ordinary case for a call that succeeded, and for every
row written before this revision.

Revision ID: b7c41d90e2a5
Revises: d5f0b31c7a62
Create Date: 2026-09-27 13:10:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b7c41d90e2a5'
down_revision = 'd5f0b31c7a62'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('error_detail', sa.Text()))
        batch_op.add_column(sa.Column('attempt', sa.Integer()))
        batch_op.add_column(sa.Column('review_cycle', sa.Integer()))


def downgrade() -> None:
    with op.batch_alter_table('model_runs', schema=None) as batch_op:
        batch_op.drop_column('review_cycle')
        batch_op.drop_column('attempt')
        batch_op.drop_column('error_detail')
