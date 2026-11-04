"""Apple build-to-order options: laptop_options and laptop_option_rules (step 10c)

Revision ID: f2a7c4e9b1d3
Revises: e4b8d2f6a9c1
Create Date: 2026-11-04

Additive: two new tables, nothing existing is touched (laptop_customizations is dropped
later, in step (g)). Both FKs cascade on delete, because the rows are derived from
Apple's configurator and mean nothing without their laptop.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "f2a7c4e9b1d3"
down_revision = "e4b8d2f6a9c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laptop_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("option_group", sqlmodel.AutoString(length=20), nullable=False),
        sa.Column("value", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("label", sqlmodel.AutoString(), nullable=True),
        sa.Column("add_on_rm", sa.Float(), nullable=True),
        sa.Column("is_included", sa.Boolean(), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("cpu_model", sqlmodel.AutoString(), nullable=True),
        sa.Column("gpu_model", sqlmodel.AutoString(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("laptop_id", "option_group", "value", name="uq_laptop_options_laptop_group_value"),
    )
    op.create_index("ix_laptop_options_laptop_id", "laptop_options", ["laptop_id"])
    op.create_table(
        "laptop_option_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_group", sqlmodel.AutoString(length=20), nullable=False),
        sa.Column("trigger_value", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("forced_group", sqlmodel.AutoString(length=20), nullable=False),
        sa.Column("forced_value", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("laptop_id", "trigger_group", "trigger_value", "forced_group",
                            name="uq_laptop_option_rules_trigger_forced"),
    )
    op.create_index("ix_laptop_option_rules_laptop_id", "laptop_option_rules", ["laptop_id"])


def downgrade() -> None:
    op.drop_index("ix_laptop_option_rules_laptop_id", table_name="laptop_option_rules")
    op.drop_table("laptop_option_rules")
    op.drop_index("ix_laptop_options_laptop_id", table_name="laptop_options")
    op.drop_table("laptop_options")
