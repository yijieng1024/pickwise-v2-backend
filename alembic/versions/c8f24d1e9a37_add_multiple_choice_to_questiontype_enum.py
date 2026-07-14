"""add multiple_choice to questiontype enum

Revision ID: c8f24d1e9a37
Revises: b3d91a4c72e0
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c8f24d1e9a37'
down_revision: Union[str, Sequence[str], None] = 'b3d91a4c72e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # QuestionType gained MULTIPLE_CHOICE in the model; the DB column is a
    # native Postgres enum, so the type must learn the new value too.
    # (PG 12+ allows ADD VALUE inside a transaction as long as the new value
    # isn't used in the same transaction.)
    op.execute("ALTER TYPE questiontype ADD VALUE IF NOT EXISTS 'MULTIPLE_CHOICE'")


def downgrade() -> None:
    # Postgres cannot drop a single value from an enum type. Rebuilding the
    # type is only safe if no row uses MULTIPLE_CHOICE — leave as no-op.
    pass
