"""merge two heads

Revision ID: e482248b8968
Revises: 65e8c344a0e4, f3d8e2c1b5a9
Create Date: 2026-06-10 17:27:19.231909

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e482248b8968'
down_revision: Union[str, Sequence[str], None] = ('65e8c344a0e4', 'f3d8e2c1b5a9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
