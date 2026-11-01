"""one row per model call of an agent turn

Revision ID: e7b3c9d1a4f6
Revises: d4a8b2c6e1f7
Create Date: 2026-11-01

Additive only: a new table. The investigation of run b6471466 could not say
which model made which step, because agent_run_logs is per turn and
llm_usage_logs is per model and only sees calls that succeeded.

agent_run_logs.fallback_triggered changes meaning in code, not in the schema:
new rows mean "a backup answered at least one step", rows before this mean "a
backup was started". They cannot be told apart, so they are left as they are.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

revision = "e7b3c9d1a4f6"
down_revision = "d4a8b2c6e1f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_model_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("chain_position", sa.Integer(), nullable=False),
        sa.Column("is_fallback", sa.Boolean(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("error_type", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run_logs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_model_call_order"),
    )


def downgrade() -> None:
    op.drop_table("agent_model_calls")
