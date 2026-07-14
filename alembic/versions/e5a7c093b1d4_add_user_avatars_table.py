"""add user_avatars table

Revision ID: e5a7c093b1d4
Revises: c8f24d1e9a37
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'e5a7c093b1d4'
down_revision: Union[str, Sequence[str], None] = 'c8f24d1e9a37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_avatars',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('content_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_avatars_user_id'), 'user_avatars', ['user_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_user_avatars_user_id'), table_name='user_avatars')
    op.drop_table('user_avatars')
