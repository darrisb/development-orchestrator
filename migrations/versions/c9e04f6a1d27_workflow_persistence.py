"""workflow persistence, pause requests and escalation intents (phase K)

Three unrelated-looking changes that are one change: the workflow becoming a
thing that can be stopped and started again.

``workflow_checkpoints`` and ``workflow_writes`` hold LangGraph's own state so
a run keeps its place across an orchestrator restart (section 27). They are the
only tables holding opaque bytes, and they are disposable: losing one costs a
run its position in the graph, never its history, which lives in the tables
around them.

``pause_requests`` separates "somebody asked for this to stop" from "this is
stopped" (section 28). The status is the workflow's to write; the request is
anyone's.

``human_escalations.resolution_intent`` records which offered option a person
chose, so an answer can restart something instead of sitting in a column
nobody reads (concern 32). Existing rows get NULL, which is the truth: they
were answered before the orchestrator could act on an answer, and they can
only be dismissed.

Revision ID: c9e04f6a1d27
Revises: b4d7c2e51a08
Create Date: 2026-09-26 18:05:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c9e04f6a1d27'
down_revision = 'b4d7c2e51a08'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pause_requests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('requested_by', sa.String(length=200), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column('honoured_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['project_id'],
            ['projects.id'],
            name=op.f('fk_pause_requests_project_id_projects'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['task_id'],
            ['tasks.id'],
            name=op.f('fk_pause_requests_task_id_tasks'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_pause_requests')),
    )
    op.create_index(
        'ix_pause_requests_project_released', 'pause_requests', ['project_id', 'released_at']
    )

    op.create_table(
        'workflow_checkpoints',
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=200), nullable=False),
        sa.Column('checkpoint_id', sa.String(length=64), nullable=False),
        sa.Column('parent_checkpoint_id', sa.String(length=64), nullable=True),
        sa.Column('checkpoint_type', sa.String(length=50), nullable=False),
        sa.Column('checkpoint', sa.LargeBinary(), nullable=False),
        sa.Column('metadata_type', sa.String(length=50), nullable=False),
        sa.Column('checkpoint_metadata', sa.LargeBinary(), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            'thread_id', 'checkpoint_ns', 'checkpoint_id', name=op.f('pk_workflow_checkpoints')
        ),
    )
    op.create_index(
        'ix_workflow_checkpoints_thread',
        'workflow_checkpoints',
        ['thread_id', 'checkpoint_ns', 'checkpoint_id'],
    )

    op.create_table(
        'workflow_writes',
        sa.Column('thread_id', sa.String(length=64), nullable=False),
        sa.Column('checkpoint_ns', sa.String(length=200), nullable=False),
        sa.Column('checkpoint_id', sa.String(length=64), nullable=False),
        sa.Column('task_id', sa.String(length=64), nullable=False),
        sa.Column('idx', sa.Integer(), nullable=False),
        sa.Column('channel', sa.String(length=200), nullable=False),
        sa.Column('value_type', sa.String(length=50), nullable=False),
        sa.Column('value', sa.LargeBinary(), nullable=False),
        sa.Column('task_path', sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint(
            'thread_id',
            'checkpoint_ns',
            'checkpoint_id',
            'task_id',
            'idx',
            name=op.f('pk_workflow_writes'),
        ),
    )

    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('resolution_intent', sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('human_escalations', schema=None) as batch_op:
        batch_op.drop_column('resolution_intent')
    op.drop_table('workflow_writes')
    op.drop_index('ix_workflow_checkpoints_thread', table_name='workflow_checkpoints')
    op.drop_table('workflow_checkpoints')
    op.drop_index('ix_pause_requests_project_released', table_name='pause_requests')
    op.drop_table('pause_requests')
