"""add status to laptops

Revision ID: d9e1f4a86c27
Revises: c4d8b31f7a55
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'd9e1f4a86c27'
down_revision: Union[str, Sequence[str], None] = 'c4d8b31f7a55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Catalog listing state: 'active' / 'inactive' / 'suspended'. Plain VARCHAR
    # (like users.status) so a new state needs no ALTER TYPE. Every existing
    # row backfills to 'active' via the server_default, which is kept on the
    # column so inserts that predate the model change still land valid.
    op.add_column(
        'laptops',
        sa.Column(
            'status',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='active',
        ),
    )


def downgrade() -> None:
    op.drop_column('laptops', 'status')
