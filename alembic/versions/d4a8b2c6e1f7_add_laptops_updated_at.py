"""laptops.updated_at: when the catalog last changed

The Dashboard marks PickScore stale when it is older than the latest catalog
change, and laptops had no write timestamp to compare against. Set on every
ORM insert and update by the mapper event in laptop_models.py. Backfilled from
created_at (stored without a time zone, so read as UTC).

Revision ID: d4a8b2c6e1f7
Revises: c3f7a9e2d5b1
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = "d4a8b2c6e1f7"
down_revision = "c3f7a9e2d5b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("laptops", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE laptops SET updated_at = created_at AT TIME ZONE 'UTC'")
    op.alter_column(
        "laptops", "updated_at", nullable=False, server_default=sa.text("now()")
    )


def downgrade() -> None:
    op.drop_column("laptops", "updated_at")
