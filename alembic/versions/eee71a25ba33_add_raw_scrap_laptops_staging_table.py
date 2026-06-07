"""add raw_scrap_laptops staging table

Revision ID: eee71a25ba33
Revises: 2be2003ece47
Create Date: 2026-06-07 16:58:04.904578

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'eee71a25ba33'
down_revision: Union[str, Sequence[str], None] = '2be2003ece47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('raw_scrap_laptops',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=False),
        sa.Column('brand', sa.String(), nullable=False),
        sa.Column('raw_product_name', sa.String(), nullable=False),
        sa.Column('raw_price_rm', sa.Float(), nullable=False),
        sa.Column('image_url', sa.String(), nullable=True),
        sa.Column('raw_specs_dump', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('processing_status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_raw_scrap_laptops_source_url'), 'raw_scrap_laptops', ['source_url'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_raw_scrap_laptops_source_url'), table_name='raw_scrap_laptops')
    op.drop_table('raw_scrap_laptops')