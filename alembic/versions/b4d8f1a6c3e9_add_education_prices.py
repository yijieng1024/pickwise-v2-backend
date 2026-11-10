"""Education prices (step 10d): laptop_edu_prices and Edu columns on laptop_options

Revision ID: b4d8f1a6c3e9
Revises: f7c3a9e2d5b8
Create Date: 2026-11-10

Additive only: one new table and three nullable columns. Nothing existing is changed,
so the app before this revision runs unchanged on the migrated schema.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "b4d8f1a6c3e9"
down_revision = "f7c3a9e2d5b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laptop_edu_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("price_rm", sa.Float(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
        sa.Column("source_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("laptop_id"),
    )
    op.add_column("laptop_options", sa.Column("edu_add_on_rm", sa.Float(), nullable=True))
    op.add_column("laptop_options", sa.Column("edu_kit_price_rm", sa.Float(), nullable=True))
    op.add_column("laptop_options", sa.Column("edu_fetched_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("laptop_options", "edu_fetched_at")
    op.drop_column("laptop_options", "edu_kit_price_rm")
    op.drop_column("laptop_options", "edu_add_on_rm")
    op.drop_table("laptop_edu_prices")
