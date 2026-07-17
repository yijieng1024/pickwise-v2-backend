"""add laptop_pick_scores table

Revision ID: a91f3c5d80e2
Revises: e5a7c093b1d4
Create Date: 2026-07-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = 'a91f3c5d80e2'
down_revision: Union[str, Sequence[str], None] = 'e5a7c093b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'laptop_pick_scores',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('laptop_id', sa.Uuid(), nullable=False),
        sa.Column('use_case', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('breakdown', sa.JSON(), nullable=True),
        sa.Column('flags', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['laptop_id'], ['laptops.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('laptop_id', 'use_case', name='uq_laptop_pick_scores_laptop_use_case'),
    )
    op.create_index(op.f('ix_laptop_pick_scores_laptop_id'), 'laptop_pick_scores', ['laptop_id'], unique=False)
    op.create_index(op.f('ix_laptop_pick_scores_use_case'), 'laptop_pick_scores', ['use_case'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_laptop_pick_scores_use_case'), table_name='laptop_pick_scores')
    op.drop_index(op.f('ix_laptop_pick_scores_laptop_id'), table_name='laptop_pick_scores')
    op.drop_table('laptop_pick_scores')
