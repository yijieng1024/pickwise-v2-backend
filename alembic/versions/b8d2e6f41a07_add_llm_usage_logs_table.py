"""add llm_usage_logs table

Revision ID: b8d2e6f41a07
Revises: a3f7c521d9b4
Create Date: 2026-10-14

One row per LLM call (per turn for the agent) across every feature, so token
spend has one place to be summed. Backfilled from agent_run_logs, the only
place tokens were recorded before this.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "b8d2e6f41a07"
down_revision = "a3f7c521d9b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("feature", sqlmodel.sql.sqltypes.AutoString(length=50), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_usage_logs_feature"), "llm_usage_logs", ["feature"])
    op.create_index(op.f("ix_llm_usage_logs_user_id"), "llm_usage_logs", ["user_id"])
    op.create_index(op.f("ix_llm_usage_logs_created_at"), "llm_usage_logs", ["created_at"])

    # Agent history. The model was gemma-4-31b-it for every logged run —
    # agent_run_logs never stored it, so this is the one model it could be.
    op.execute(
        """
        INSERT INTO llm_usage_logs (id, feature, model, input_tokens, output_tokens, user_id, created_at)
        SELECT gen_random_uuid(), 'agent', 'gemma-4-31b-it',
               COALESCE(input_tokens, 0), COALESCE(output_tokens, 0), user_id, created_at
        FROM agent_run_logs
        WHERE COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0
        """
    )


def downgrade() -> None:
    # Usage telemetry only; agent_run_logs still holds the agent's numbers.
    op.drop_index(op.f("ix_llm_usage_logs_created_at"), table_name="llm_usage_logs")
    op.drop_index(op.f("ix_llm_usage_logs_user_id"), table_name="llm_usage_logs")
    op.drop_index(op.f("ix_llm_usage_logs_feature"), table_name="llm_usage_logs")
    op.drop_table("llm_usage_logs")
