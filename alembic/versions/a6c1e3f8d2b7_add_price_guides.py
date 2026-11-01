"""product guide staging tables, manufacturer_sku, and where a price came from

Revision ID: a6c1e3f8d2b7
Revises: f2c6d8a4b1e9
Create Date: 2026-11-01

Additive only: two new tables, and nullable columns with no backfill. An old
price-history row keeps a null source, which counts as "not a guide", so the
import never overwrites it.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "a6c1e3f8d2b7"
down_revision = "f2c6d8a4b1e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_guides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column("filename", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("pdf_sha256", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("edition_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pdf_sha256"),
    )
    op.create_index(op.f("ix_price_guides_kind"), "price_guides", ["kind"])
    op.create_table(
        "price_guide_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("guide_id", sa.Uuid(), nullable=False),
        sa.Column("sku", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model_code", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cpu", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("ram_gb", sa.Integer(), nullable=True),
        sa.Column("ssd_gb", sa.Integer(), nullable=True),
        sa.Column("price_rm", sa.Float(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["guide_id"], ["price_guides.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guide_id", "sku", name="uq_price_guide_rows_guide_sku"),
    )
    op.create_index(op.f("ix_price_guide_rows_guide_id"), "price_guide_rows", ["guide_id"])

    op.add_column("laptops", sa.Column("manufacturer_sku", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.create_index(
        "uq_laptops_manufacturer_sku", "laptops", ["manufacturer_sku"], unique=True,
        postgresql_where=sa.text("manufacturer_sku IS NOT NULL"),
    )
    op.add_column("laptop_price_history", sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=True))
    op.add_column("laptop_price_history", sa.Column("price_guide_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_laptop_price_history_price_guide_id", "laptop_price_history", "price_guides",
        ["price_guide_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_laptop_price_history_price_guide_id", "laptop_price_history", type_="foreignkey")
    op.drop_column("laptop_price_history", "price_guide_id")
    op.drop_column("laptop_price_history", "source")
    op.drop_index(
        "uq_laptops_manufacturer_sku", table_name="laptops",
        postgresql_where=sa.text("manufacturer_sku IS NOT NULL"),
    )
    op.drop_column("laptops", "manufacturer_sku")
    op.drop_index(op.f("ix_price_guide_rows_guide_id"), table_name="price_guide_rows")
    op.drop_table("price_guide_rows")
    op.drop_index(op.f("ix_price_guides_kind"), table_name="price_guides")
    op.drop_table("price_guides")
