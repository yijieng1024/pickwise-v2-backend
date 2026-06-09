"""add new tbl laptop_scrape_urls

Revision ID: 65e8c344a0e4
Revises: 3ebc95cb5885
Create Date: 2026-06-09 22:55:38.039888

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65e8c344a0e4'
down_revision: Union[str, Sequence[str], None] = '3ebc95cb5885'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('laptop_scrape_urls',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('url', sa.String(), nullable=False),
        sa.Column('brand', sa.String(), nullable=False),
        sa.Column('last_scraped_at', sa.DateTime(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_index(op.f('ix_laptop_scrape_urls_url'), 'laptop_scrape_urls', ['url'], unique=True)


def downgrade() -> None:
    # Drop the index before dropping the table
    op.drop_index(op.f('ix_laptop_scrape_urls_url'), table_name='laptop_scrape_urls')
    op.drop_table('laptop_scrape_urls')
