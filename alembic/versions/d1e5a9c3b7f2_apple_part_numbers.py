"""Apple part numbers on a laptop, and room for the apple-buy-page price source

Revision ID: d1e5a9c3b7f2
Revises: c8d2f5a7b1e4
Create Date: 2026-11-03

Additive: `laptops.part_numbers` is a nullable JSONB list (Apple gives each colour of one
configuration its own part number, so one row holds several), and
`laptop_price_history.source` widens from 10 to 20 characters for "apple-buy-page".
Widening a varchar rewrites nothing. The downgrade cuts any longer source to 10.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d1e5a9c3b7f2"
down_revision = "c8d2f5a7b1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("laptops", sa.Column("part_numbers", postgresql.JSONB(), nullable=True))
    op.alter_column("laptop_price_history", "source", type_=sa.String(20), existing_type=sa.String(10),
                    existing_nullable=True)


def downgrade() -> None:
    op.execute("UPDATE laptop_price_history SET source = left(source, 10) WHERE length(source) > 10")
    op.alter_column("laptop_price_history", "source", type_=sa.String(10), existing_type=sa.String(20),
                    existing_nullable=True)
    op.drop_column("laptops", "part_numbers")
