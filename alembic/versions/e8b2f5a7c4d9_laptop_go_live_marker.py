"""laptops.go_live_held and go_live_failures: the go-live check's marker (step 11)

Revision ID: e8b2f5a7c4d9
Revises: d1b6f3a8c2e4
Create Date: 2026-11-13

Additive only: a NOT NULL boolean with a server default of false, and a nullable
JSONB list. Every existing laptop gets false, so the go-live check holds none of
them: it acts only on laptops the processor inserts from now on.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e8b2f5a7c4d9"
down_revision = "d1b6f3a8c2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "laptops",
        sa.Column("go_live_held", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("laptops", sa.Column("go_live_failures", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("laptops", "go_live_failures")
    op.drop_column("laptops", "go_live_held")
