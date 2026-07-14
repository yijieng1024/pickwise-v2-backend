"""add google login fields to users

Revision ID: b3d91a4c72e0
Revises: ffb4429867dd
Create Date: 2026-07-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'b3d91a4c72e0'
down_revision: Union[str, Sequence[str], None] = 'ffb4429867dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # social-login accounts have no local password
    op.alter_column('users', 'password', existing_type=sa.VARCHAR(), nullable=True)
    op.add_column(
        'users',
        sa.Column(
            'auth_provider',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='local',
        ),
    )
    op.add_column(
        'users',
        sa.Column('provider_sub', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )
    op.create_index(op.f('ix_users_provider_sub'), 'users', ['provider_sub'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_provider_sub'), table_name='users')
    op.drop_column('users', 'provider_sub')
    op.drop_column('users', 'auth_provider')
    # NOTE: fails if any Google-created user has password IS NULL —
    # delete or backfill those rows before downgrading
    op.alter_column('users', 'password', existing_type=sa.VARCHAR(), nullable=False)
