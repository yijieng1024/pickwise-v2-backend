"""Job records (step 11, commit 2): who started a job and where its worker runs

Revision ID: c9a4e7b2d1f5
Revises: b4d8f1a6c3e9
Create Date: 2026-11-12

Additive only: two NOT NULL columns with server defaults, so every existing row
reads as started by an admin and running on Render, which is what they all were.
The app before this revision inserts without the columns and gets the defaults.
"""
from alembic import op
import sqlalchemy as sa

revision = "c9a4e7b2d1f5"
down_revision = "b4d8f1a6c3e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("background_jobs", sa.Column("started_by", sa.String(), nullable=False, server_default="admin"))
    op.add_column(
        "background_jobs",
        sa.Column("runs_outside_render", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("background_jobs", "runs_outside_render")
    op.drop_column("background_jobs", "started_by")
