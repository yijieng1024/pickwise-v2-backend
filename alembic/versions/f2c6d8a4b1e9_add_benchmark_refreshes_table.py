"""one row per successful PassMark refresh

Revision ID: f2c6d8a4b1e9
Revises: e7b3c9d1a4f6
Create Date: 2026-11-01

Additive only: a new table, which feeds the Dashboard's PassMark date and the
Pick Score footnote. One row per run rather than a date per benchmark row: each
run rewrites all ~6,000 rows, so every row would carry the same date.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "f2c6d8a4b1e9"
down_revision = "e7b3c9d1a4f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_refreshes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("source_updated_text", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_benchmark_refreshes_source"), "benchmark_refreshes", ["source"])


def downgrade() -> None:
    op.drop_index(op.f("ix_benchmark_refreshes_source"), table_name="benchmark_refreshes")
    op.drop_table("benchmark_refreshes")
