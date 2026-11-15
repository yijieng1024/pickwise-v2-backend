"""laptop_skus.last_seen_at: when a weekly run last saw the SKU on its page

Revision ID: b5e2c8f1a7d3
Revises: a2d6f8c4e1b9
Create Date: 2026-11-15

Additive only: one nullable column. Null means not seen since tracking started;
app/scripts/backfill_sku_last_seen.py fills it from one fetch of the ROG and HP pages.
"""
from alembic import op
import sqlalchemy as sa

revision = "b5e2c8f1a7d3"
down_revision = "a2d6f8c4e1b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("laptop_skus", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("laptop_skus", "last_seen_at")
