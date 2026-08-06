"""add background_jobs table

State for long-running admin batch operations (bulk scrape, AI processing,
tagging) that were converted from blocking requests into polled background
jobs. See app/common/job_model.py for why this is a table rather than an
in-process registry.

Revision ID: c4d8b31f7a55
Revises: b7e4a2f16c93
Create Date: 2026-08-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c4d8b31f7a55'
down_revision: Union[str, Sequence[str], None] = 'b7e4a2f16c93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'background_jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('job_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('total_count', sa.Integer(), nullable=False),
        sa.Column('processed_count', sa.Integer(), nullable=False),
        sa.Column('succeeded_count', sa.Integer(), nullable=False),
        sa.Column('failed_count', sa.Integer(), nullable=False),
        sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('estimated_seconds', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_background_jobs_job_type'), 'background_jobs', ['job_type'], unique=False)
    op.create_index(op.f('ix_background_jobs_status'), 'background_jobs', ['status'], unique=False)
    op.create_index(op.f('ix_background_jobs_created_by'), 'background_jobs', ['created_by'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_background_jobs_created_by'), table_name='background_jobs')
    op.drop_index(op.f('ix_background_jobs_status'), table_name='background_jobs')
    op.drop_index(op.f('ix_background_jobs_job_type'), table_name='background_jobs')
    op.drop_table('background_jobs')
