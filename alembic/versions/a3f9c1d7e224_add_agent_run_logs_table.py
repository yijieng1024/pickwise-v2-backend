"""add agent_run_logs table

Revision ID: a3f9c1d7e224
Revises: f1a2b3c4d5e6
Create Date: 2026-08-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a3f9c1d7e224'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_run_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('conversation_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('user_message', sa.Text(), nullable=False),
        sa.Column('reply_text', sa.Text(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_agent_run_logs_conversation_id'), 'agent_run_logs', ['conversation_id'])
    op.create_index(op.f('ix_agent_run_logs_user_id'), 'agent_run_logs', ['user_id'])
    op.create_index(op.f('ix_agent_run_logs_status'), 'agent_run_logs', ['status'])
    op.create_index(op.f('ix_agent_run_logs_created_at'), 'agent_run_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_run_logs_created_at'), table_name='agent_run_logs')
    op.drop_index(op.f('ix_agent_run_logs_status'), table_name='agent_run_logs')
    op.drop_index(op.f('ix_agent_run_logs_user_id'), table_name='agent_run_logs')
    op.drop_index(op.f('ix_agent_run_logs_conversation_id'), table_name='agent_run_logs')
    op.drop_table('agent_run_logs')
