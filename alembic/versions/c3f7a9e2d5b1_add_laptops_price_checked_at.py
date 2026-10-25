"""laptops.price_checked_at: "Last Checked" for the official price

LaptopPriceHistory records only price changes, so it cannot say when an
unchanged price was last confirmed. NULL = no trustworthy check date; the
details page then shows no date. Backfilled separately by
app/scripts/backfill_price_checked.py (dry run by default), not here.

Revision ID: c3f7a9e2d5b1
Revises: b8e2d4f6a1c3
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "c3f7a9e2d5b1"
down_revision = "b8e2d4f6a1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("laptops", sa.Column("price_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("laptops", "price_checked_at")
