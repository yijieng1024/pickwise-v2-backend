"""background_jobs.updated_at: when a job last wrote progress (step 11)

Revision ID: d1b6f3a8c2e4
Revises: c9a4e7b2d1f5
Create Date: 2026-11-12

Additive only: one NOT NULL column with a server default of now(). Existing rows
get the migration time, which changes nothing for them: the column is read only
for active jobs that run outside Render, and none exists yet.
"""
from alembic import op
import sqlalchemy as sa

revision = "d1b6f3a8c2e4"
down_revision = "c9a4e7b2d1f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "background_jobs",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_column("background_jobs", "updated_at")
