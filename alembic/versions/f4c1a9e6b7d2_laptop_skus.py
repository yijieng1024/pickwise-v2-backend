"""laptop_skus: every SKU a laptop is sold under (step 11, decisions 15 and 16)

Revision ID: f4c1a9e6b7d2
Revises: e8b2f5a7c4d9
Create Date: 2026-11-13

Additive only: one new table. One row per SKU, with its laptop, the colour name its
page gives and the price that page last showed. `laptops.manufacturer_sku` is not
touched and stays the SKU the product guides match on. The table starts empty: the
processor writes a row when it next sees a SKU.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "f4c1a9e6b7d2"
down_revision = "e8b2f5a7c4d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laptop_skus",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("colour", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("price_rm", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )
    op.create_index(op.f("ix_laptop_skus_laptop_id"), "laptop_skus", ["laptop_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_laptop_skus_laptop_id"), table_name="laptop_skus")
    op.drop_table("laptop_skus")
