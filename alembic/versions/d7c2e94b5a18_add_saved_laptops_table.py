"""add saved_laptops table

Revision ID: d7c2e94b5a18
Revises: a91f3c5d80e2
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd7c2e94b5a18'
down_revision: Union[str, Sequence[str], None] = 'a91f3c5d80e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'saved_laptops',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('laptop_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['laptop_id'], ['laptops.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'laptop_id', name='uq_saved_laptops_user_laptop'),
    )
    op.create_index(op.f('ix_saved_laptops_user_id'), 'saved_laptops', ['user_id'])
    op.create_index(op.f('ix_saved_laptops_laptop_id'), 'saved_laptops', ['laptop_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_saved_laptops_laptop_id'), table_name='saved_laptops')
    op.drop_index(op.f('ix_saved_laptops_user_id'), table_name='saved_laptops')
    op.drop_table('saved_laptops')
