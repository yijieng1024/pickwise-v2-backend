"""make laptop_pick_scores.score nullable for withheld scores

Revision ID: 2d909406ade7
Revises: a3f7d21c6b84
Create Date: 2026-09-15 19:28:16.552970

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2d909406ade7'
down_revision: Union[str, Sequence[str], None] = 'a3f7d21c6b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """laptop_pick_scores.score becomes nullable.

    A withheld score (ADR-0016: both CPU and GPU unresolved) is written as a
    row with score NULL and flags.score_withheld true, NOT as a missing row.
    A missing row and a withheld score look identical to a caller, and that
    ambiguity is the thing being fixed -- the same defect as price_rm = 0
    meaning both "free" and "unknown".

    Widening only: every existing row has a score and stays valid.
    """
    op.alter_column(
        "laptop_pick_scores", "score", existing_type=sa.Integer(), nullable=True
    )


def downgrade() -> None:
    """Narrowing back to NOT NULL. Any withheld row would block this, so they
    are dropped first -- a withheld score cannot be represented in the old
    schema, and inventing a number for it is exactly what this ADR forbids."""
    op.execute("DELETE FROM laptop_pick_scores WHERE score IS NULL")
    op.alter_column(
        "laptop_pick_scores", "score", existing_type=sa.Integer(), nullable=False
    )
