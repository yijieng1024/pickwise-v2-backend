"""pipeline_settings: the weekly pipeline's settings, edited by the admin (step 11, item 8)

Revision ID: a2d6f8c4e1b9
Revises: f4c1a9e6b7d2
Create Date: 2026-11-13

Additive only: one new table and its one row, holding the defaults (off, Sunday, no
report email, 30% price jump, every site on). Secrets are never stored here.
"""
from alembic import op
import sqlalchemy as sa

revision = "a2d6f8c4e1b9"
down_revision = "f4c1a9e6b7d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "pipeline_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("weekday", sa.String(length=9), nullable=False),
        sa.Column("report_email", sa.String(length=254), nullable=False),
        sa.Column("price_jump_percent", sa.Integer(), nullable=False),
        sa.Column("asus_enabled", sa.Boolean(), nullable=False),
        sa.Column("rog_enabled", sa.Boolean(), nullable=False),
        sa.Column("hp_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(table, [{
        "id": 1, "enabled": False, "weekday": "sunday", "report_email": "", "price_jump_percent": 30,
        "asus_enabled": True, "rog_enabled": True, "hp_enabled": True,
    }])


def downgrade() -> None:
    op.drop_table("pipeline_settings")
