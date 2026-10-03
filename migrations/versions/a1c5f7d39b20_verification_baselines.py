"""verification baselines: known failures per tree (concern 78, stage 2)

Stage 1 told the coder to stop measuring the project's baseline. Something has
to, or the orchestrator's own non-zero exit code is indistinguishable from a
regression, and every pre-existing failure in the tree becomes a repair
instruction the coder cannot act on.

This table is the durable evidence that makes the distinction possible: for one
project, one commit, one verification command, what that command returned and
which failures it identified. The unique key is the provenance -- a row is only
usable for a candidate whose worktree started at exactly ``baseline_sha`` and
whose failing command text matches ``command`` under the same
``verification_type``. Anything else classifies as unclassified, which is the
state that preserves today's behaviour.

Nothing is backfilled. There is no commit in any existing project for which
this orchestrator has recorded failure identities, and inventing a row would be
worse than having none: a fabricated baseline makes real regressions look
known. Existing rows elsewhere are untouched, and a project with no rows here
behaves exactly as it did before stage 2.

Revision ID: a1c5f7d39b20
Revises: f7b2d4c80e13
Create Date: 2026-10-03 09:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a1c5f7d39b20'
down_revision = 'f7b2d4c80e13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'verification_baselines',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('baseline_sha', sa.String(length=64), nullable=False),
        sa.Column('verification_type', sa.String(length=32), nullable=False),
        sa.Column('command', sa.Text(), nullable=False),
        # Provenance: the same command under a different worker profile runs in
        # a different image and is a different measurement.
        sa.Column('worker_profile', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column(
            'failure_identities',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        # Defaults to true so that a row written by an older code path cannot
        # read as "this command failed and nothing failed", which is the one
        # combination that would turn a regression into a known failure.
        sa.Column(
            'failures_available',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('extractor', sa.String(length=64), nullable=True),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('stdout_artifact', sa.String(length=1024), nullable=True),
        sa.Column('source_task_run_id', sa.Uuid(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        # SET NULL, not CASCADE: the evidence outlives the run that gathered it.
        sa.ForeignKeyConstraint(
            ['source_task_run_id'], ['task_runs.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'project_id', 'baseline_sha', 'verification_type', 'command'
        ),
    )
    op.create_index(
        'ix_verification_baselines_lookup',
        'verification_baselines',
        ['project_id', 'baseline_sha'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_verification_baselines_lookup', table_name='verification_baselines'
    )
    op.drop_table('verification_baselines')
