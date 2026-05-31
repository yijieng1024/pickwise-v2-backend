"""Add user profile fields and tech_savviness to preferences

Revision ID: c7d3e2f1a4b9
Revises: 3a1f2e3d4c5b
Create Date: 2026-05-31 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c7d3e2f1a4b9'
down_revision: Union[str, Sequence[str], None] = '3a1f2e3d4c5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    # Add columns to users table
    op.add_column('users', sa.Column('birthday', sa.Date(), nullable=True))
    op.add_column('users', sa.Column('gender', sa.String(), nullable=True))
    op.add_column('users', sa.Column('occupation', sa.String(), nullable=True))
    
    # Add tech_savviness column to laptop_user_preference table
    op.add_column('laptop_user_preference', sa.Column('tech_savviness', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    
    # Remove columns from users table
    op.drop_column('users', 'occupation')
    op.drop_column('users', 'gender')
    op.drop_column('users', 'birthday')
    
    # Remove tech_savviness column from laptop_user_preference table
    op.drop_column('laptop_user_preference', 'tech_savviness')
