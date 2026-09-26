"""experience capture: lesson approval state and training index (phase L)

Two changes that are one change: making the orchestrator's history usable rather
than merely retained.

``lessons`` gains the state section 32's six rules need. A candidate is
``proposed`` and invisible to every retrieval site; only ``approved`` lessons
are put in front of a coder. ``occurrences`` counts the runs that have raised
the same finding, which is the evidence behind rule 2's "prefer recurring", and
``requirement_id``/``source_file`` store the finding's identity so a repeat
finds the existing lesson instead of proposing a duplicate. Existing rows become
``proposed``: they were inserted by hand or by an earlier phase with no approval
step behind them, and treating them as approved would put unvetted advice in a
prompt on the strength of a migration.

``training_examples`` indexes what section 34 asks to preserve for an accepted
run. Every row is written ``captured``; the specification says not to train on
every accepted example, so selection is left to the future curation process.

Revision ID: f1a83c6d2e47
Revises: c9e04f6a1d27
Create Date: 2026-09-26 19:20:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f1a83c6d2e47'
down_revision = 'c9e04f6a1d27'
branch_labels = None
depends_on = None

_LESSON_STATUSES = "proposed", "approved", "rejected", "retired"
_TRAINING_STATUSES = "captured", "selected", "excluded"


def _sql_list(values: tuple[str, ...]) -> str:
    """A Python tuple rendered as a SQL value list."""
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table('lessons', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'status',
                sa.String(length=16),
                nullable=False,
                server_default='proposed',
            )
        )
        batch_op.add_column(
            sa.Column('occurrences', sa.Integer(), nullable=False, server_default='1')
        )
        batch_op.add_column(sa.Column('requirement_id', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('source_file', sa.String(length=1024), nullable=True))
        batch_op.add_column(
            sa.Column('source_run_id', sa.Uuid(), nullable=True)
        )
        # Which run last contributed to ``occurrences``, so the count stays a
        # count of *distinct* runs. Without it a run that re-raises a finding is
        # counted again on every proposal, and the inflated count is what drives
        # confidence.
        batch_op.add_column(
            sa.Column('last_seen_run_id', sa.Uuid(), nullable=True)
        )
        batch_op.add_column(sa.Column('approved_by', sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column('rejection_reason', sa.Text(), nullable=True))
        # A check constraint rather than an enum type, for the same reason
        # ``reviews.confidence_range`` exists: the vocabulary is fixed in
        # ``domain.enums`` and the database refuses anything else, so a status
        # written by hand cannot become a value no code reads.
        batch_op.create_check_constraint(
            'lesson_status_valid',
            f"status IN {_sql_list(_LESSON_STATUSES)}",
        )
        batch_op.create_check_constraint(
            'lesson_occurrences_positive', 'occurrences >= 1'
        )
        batch_op.create_foreign_key(
            op.f('fk_lessons_source_run_id_task_runs'),
            'task_runs',
            ['source_run_id'],
            ['id'],
            ondelete='SET NULL',
        )
        # Same reasoning as ``source_run_id``, and the same ``ondelete``: a run
        # row can go (a project deleted, a test purged) while the lesson it
        # taught stays, so the pointer nulls rather than taking the lesson with
        # it. The column is added bare above and constrained here only because
        # ``lessons`` is an existing table and SQLite cannot add a foreign key to
        # one in place.
        batch_op.create_foreign_key(
            op.f('fk_lessons_last_seen_run_id_task_runs'),
            'task_runs',
            ['last_seen_run_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            'ix_lessons_status_evidence',
            ['status', 'occurrences', 'times_applied'],
            unique=False,
        )

    op.create_table(
        'training_examples',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_run_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('external_project_id', sa.String(length=100), nullable=False),
        sa.Column('external_task_id', sa.String(length=100), nullable=False),
        sa.Column('external_run_id', sa.String(length=64), nullable=False),
        sa.Column('artifact_path', sa.String(length=1024), nullable=False),
        sa.Column('manifest_sha256', sa.String(length=64), nullable=False),
        sa.Column('outcome', sa.String(length=50), nullable=False),
        sa.Column('coder_model_id', sa.Uuid(), nullable=True),
        sa.Column('reviewer_model', sa.String(length=200), nullable=True),
        sa.Column('prompt_version', sa.String(length=50), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('review_cycles', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column(
            'status',
            sa.String(length=16),
            nullable=False,
            server_default='captured',
        ),
        sa.Column('exclusion_reason', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['coder_model_id'],
            ['models.id'],
            name=op.f('fk_training_examples_coder_model_id_models'),
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['project_id'],
            ['projects.id'],
            name=op.f('fk_training_examples_project_id_projects'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['task_id'],
            ['tasks.id'],
            name=op.f('fk_training_examples_task_id_tasks'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['task_run_id'],
            ['task_runs.id'],
            name=op.f('fk_training_examples_task_run_id_task_runs'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_run_id'),
        sa.CheckConstraint(
            f"status IN {_sql_list(_TRAINING_STATUSES)}", name='training_status_valid'
        ),
    )
    op.create_index(
        'ix_training_examples_status', 'training_examples', ['status'], unique=False
    )
    op.create_index(
        'ix_training_examples_project',
        'training_examples',
        ['project_id', 'created_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_training_examples_project', table_name='training_examples')
    op.drop_index('ix_training_examples_status', table_name='training_examples')
    op.drop_table('training_examples')

    with op.batch_alter_table('lessons', schema=None) as batch_op:
        batch_op.drop_constraint('lesson_occurrences_positive', type_='check')
        batch_op.drop_constraint('lesson_status_valid', type_='check')
        batch_op.drop_index('ix_lessons_status_evidence')
        batch_op.drop_constraint(op.f('fk_lessons_last_seen_run_id_task_runs'), type_='foreignkey')
        batch_op.drop_constraint(op.f('fk_lessons_source_run_id_task_runs'), type_='foreignkey')
        batch_op.drop_column('rejection_reason')
        batch_op.drop_column('approved_at')
        batch_op.drop_column('approved_by')
        batch_op.drop_column('last_seen_run_id')
        batch_op.drop_column('source_run_id')
        batch_op.drop_column('source_file')
        batch_op.drop_column('requirement_id')
        batch_op.drop_column('occurrences')
        batch_op.drop_column('status')
