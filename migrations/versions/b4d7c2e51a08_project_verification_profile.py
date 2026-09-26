"""project verification profile (build.md section 18)

A project declares the commands the verification pipeline runs, per category.
They live on the project rather than on each task because section 18 makes
them project-controlled: a task may narrow the test suite through its own
``verify`` list, and may not invent a build.

Existing rows get an empty profile, which is the same as a manifest that
declared no ``verification:`` block: the pipeline records each category as
SKIPPED rather than claiming a pass it never saw.

Revision ID: b4d7c2e51a08
Revises: 8f2a17b4c903
Create Date: 2026-09-26 16:20:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b4d7c2e51a08'
down_revision = '8f2a17b4c903'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('verification_profile', sa.JSON(), nullable=False, server_default='{}')
        )


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('verification_profile')
