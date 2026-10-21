"""record which model answered and whether fallback fired

Revision ID: c5e9a1d7f3b2
Revises: b8d2e6f41a07
Create Date: 2026-10-21

Phase 2 of the LLM fallback work. Additive only:
  agent_run_logs.model               -- the model that wrote the reply (NULL for
                                        rows before this, and turns that errored
                                        before any LLM call completed)
  agent_run_logs.model_position      -- its place in the chain: 0 = primary,
                                        1..n = backup index (NULL as for model)
  agent_run_logs.fallback_triggered  -- any step of the turn ran on a backup
  llm_usage_logs.is_fallback         -- this row's tokens were a backup's
  llm_usage_logs.model_position      -- which position answered (0 = primary)
  app_settings.value                 -- widened VARCHAR(200) -> VARCHAR(2000)
                                        for `<use>.backup_models` JSON lists;
                                        widening is metadata-only in Postgres

The booleans get server_default false so existing rows read as "no fallback",
which is true: no fallback mechanism existed for these features before.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "c5e9a1d7f3b2"
down_revision = "b8d2e6f41a07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_run_logs",
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
    )
    op.add_column("agent_run_logs", sa.Column("model_position", sa.Integer(), nullable=True))
    op.add_column(
        "agent_run_logs",
        sa.Column("fallback_triggered", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "llm_usage_logs",
        sa.Column("is_fallback", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "llm_usage_logs",
        sa.Column("model_position", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column(
        "app_settings", "value",
        type_=sqlmodel.sql.sqltypes.AutoString(length=2000),
        existing_type=sa.VARCHAR(length=200), existing_nullable=False,
    )


def downgrade() -> None:
    # Fails if a stored value exceeds 200 chars -- shorten or delete the
    # `<use>.backup_models` rows first rather than truncating them silently.
    op.alter_column(
        "app_settings", "value",
        type_=sa.VARCHAR(length=200),
        existing_type=sqlmodel.sql.sqltypes.AutoString(length=2000), existing_nullable=False,
    )
    op.drop_column("llm_usage_logs", "model_position")
    op.drop_column("llm_usage_logs", "is_fallback")
    op.drop_column("agent_run_logs", "fallback_triggered")
    op.drop_column("agent_run_logs", "model_position")
    op.drop_column("agent_run_logs", "model")
