"""Apple build-to-order options: laptop_options, chip kit by chip kit (step 10c)

Revision ID: f2a7c4e9b1d3
Revises: e4b8d2f6a9c1
Create Date: 2026-11-04

Additive: one new table, nothing existing is touched (laptop_customizations is dropped
later, in step (g)). Rows are keyed by product, size, line and chip, not by laptop, so
there is no FK: a chip kit is shared by every standard config with that chip.
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
        sa.Column("product", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("size_inch", sa.Integer(), nullable=False),
        sa.Column("line", sqlmodel.AutoString(length=20), nullable=False),
        sa.Column("chip", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("option_group", sqlmodel.AutoString(length=20), nullable=False),
        sa.Column("value", sqlmodel.AutoString(length=40), nullable=False),
        sa.Column("label", sqlmodel.AutoString(), nullable=True),
        sa.Column("add_on_rm", sa.Float(), nullable=True),
        sa.Column("kit_price_rm", sa.Float(), nullable=True),
        sa.Column("is_included", sa.Boolean(), nullable=False),
        sa.Column("is_blocked", sa.Boolean(), nullable=False),
        sa.Column("cpu_model", sqlmodel.AutoString(), nullable=True),
        sa.Column("gpu_model", sqlmodel.AutoString(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product", "size_inch", "line", "chip", "option_group", "value",
                            name="uq_laptop_options_kit_group_value"),
    )
    op.create_index("ix_laptop_options_kit", "laptop_options", ["product", "size_inch", "line", "chip"])


def downgrade() -> None:
    op.drop_index("ix_laptop_options_kit", table_name="laptop_options")
    op.drop_table("laptop_options")
