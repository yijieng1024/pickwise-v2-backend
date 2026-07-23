"""add status to users

Revision ID: f1a2b3c4d5e6
Revises: d7c2e94b5a18
Create Date: 2026-07-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'd7c2e94b5a18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # admin moderation state: 'active' / 'inactive' / 'suspended', enforced at
    # login and on every authenticated request (see get_current_user)
    op.add_column(
        'users',
        sa.Column(
            'status',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='active',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'status')
