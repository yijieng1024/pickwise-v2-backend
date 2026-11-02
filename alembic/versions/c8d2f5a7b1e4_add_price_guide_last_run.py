"""the last workflow run that touched a product guide

Revision ID: c8d2f5a7b1e4
Revises: b4f7c2e9d1a3
Create Date: 2026-11-02

Additive only: two nullable columns, written by the workflow's write step. They are
how the admin page says what a finished run did, which GitHub cannot tell it.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "c8d2f5a7b1e4"
down_revision = "b4f7c2e9d1a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("price_guides", sa.Column("last_run_id", sa.BigInteger(), nullable=True))
    op.add_column("price_guides", sa.Column("last_run_note", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column("price_guides", "last_run_note")
    op.drop_column("price_guides", "last_run_id")
