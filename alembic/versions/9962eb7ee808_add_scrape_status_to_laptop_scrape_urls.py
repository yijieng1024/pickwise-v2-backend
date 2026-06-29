"""add scrape_status to laptop_scrape_urls

Revision ID: 9962eb7ee808
Revises: 453fffc97e7b
Create Date: 2026-06-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision: str = '9962eb7ee808'
down_revision: Union[str, Sequence[str], None] = '453fffc97e7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'laptop_scrape_urls',
        sa.Column(
            'scrape_status',
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default='pending',
        ),
    )


def downgrade() -> None:
    op.drop_column('laptop_scrape_urls', 'scrape_status')
