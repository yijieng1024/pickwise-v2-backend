"""price_guide_reviews: guide rows an admin has decided on

Revision ID: c7a3e9d2f4b6
Revises: b5e2c8f1a7d3
Create Date: 2026-11-15

Additive only: one new table. A reviewed row leaves the guide's waiting and differs
lists until the guide price for that laptop and SKU changes.
"""
from alembic import op
import sqlalchemy as sa

revision = "c7a3e9d2f4b6"
down_revision = "b5e2c8f1a7d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_guide_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("guide_id", sa.Uuid(), nullable=False),
        sa.Column("guide_sku", sa.String(length=64), nullable=False),
        sa.Column("guide_price_rm", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"]),
        sa.ForeignKeyConstraint(["guide_id"], ["price_guides.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("laptop_id", "guide_id", name="uq_price_guide_reviews_laptop_guide"),
    )
    op.create_index("ix_price_guide_reviews_laptop_id", "price_guide_reviews", ["laptop_id"])
    op.create_index("ix_price_guide_reviews_guide_id", "price_guide_reviews", ["guide_id"])


def downgrade() -> None:
    op.drop_index("ix_price_guide_reviews_guide_id", table_name="price_guide_reviews")
    op.drop_index("ix_price_guide_reviews_laptop_id", table_name="price_guide_reviews")
    op.drop_table("price_guide_reviews")
