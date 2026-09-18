"""add retrieval_fallback to pipeline_eval_logs

Records which retrieval path answered a request. Until now a fallback run —
the embedding API failed and _relational_fallback answered from SQL — was
inferable only from top_score landing on exactly 0.5, which collides with a
genuine embedding hit and stops working the moment that placeholder constant
moves.

server_default='false' rather than a Python-side default: the column is NOT
NULL on a populated table, so Postgres needs a value for the existing rows.
Those rows are backfilled to false, which is the right reading — every one of
them predates the flag, and a fallback run was the rare case.

Revision ID: a3f7d21c6b84
Revises: 7c66592b531e
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7d21c6b84'
down_revision: Union[str, Sequence[str], None] = '7c66592b531e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'pipeline_eval_logs',
        sa.Column(
            'retrieval_fallback',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade() -> None:
    op.drop_column('pipeline_eval_logs', 'retrieval_fallback')
