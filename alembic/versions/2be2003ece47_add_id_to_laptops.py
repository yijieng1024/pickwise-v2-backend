"""add id to laptops

Revision ID: 2be2003ece47
Revises: c7d3e2f1a4b9
Create Date: 2026-06-02 17:20:54.461687

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2be2003ece47'
down_revision: Union[str, Sequence[str], None] = 'c7d3e2f1a4b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Comment this out so it doesn't execute and crash:
    # op.add_column('laptops', sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')))
    pass

def downgrade() -> None:
    pass
