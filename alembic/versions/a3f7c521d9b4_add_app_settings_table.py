"""add app_settings table

Revision ID: a3f7c521d9b4
Revises: 2d909406ade7
Create Date: 2026-09-22

Admin-changeable runtime settings. One row today (`processor.model`), but the
table is a plain key/value store so the next one needs no migration.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "a3f7c521d9b4"
down_revision = "2d909406ade7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    # No data loss worth guarding: every value has a code default, so dropping
    # the table returns the app to its compiled-in behaviour.
    op.drop_table("app_settings")
