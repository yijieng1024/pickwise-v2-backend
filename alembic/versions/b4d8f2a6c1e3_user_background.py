"""user_background: onboarding background answers, one row per user

Revision ID: b4d8f2a6c1e3
Revises: a3c7e1f9b2d4
Create Date: 2026-11-09

Additive. Every answer column is nullable (each question is skippable). The FK to
users is NO ACTION like every other FK in the schema; DELETE /auth/me deletes the
row explicitly.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "b4d8f2a6c1e3"
down_revision = "a3c7e1f9b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_background",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("user_type", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("field_of_study", sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
        sa.Column("current_laptop", sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_background")
