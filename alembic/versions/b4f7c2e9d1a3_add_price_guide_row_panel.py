"""the panel printed for a product guide row

Revision ID: b4f7c2e9d1a3
Revises: a6c1e3f8d2b7
Create Date: 2026-11-02

Additive only: one nullable column. A row stored before it has no panel and matches
as it always did, until the same PDF is run through the workflow again, which fills
null panels and changes nothing else.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "b4f7c2e9d1a3"
down_revision = "a6c1e3f8d2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("price_guide_rows", sa.Column("panel", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("price_guide_rows", "panel")
