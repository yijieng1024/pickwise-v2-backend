"""create laptop_brands table

Revision ID: e1a2b3c4d5e6
Revises: eee71a25ba33
Create Date: 2026-06-10 12:30:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "e1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "eee71a25ba33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - create laptop_brands table."""
    op.create_table(
        "laptop_brands",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("base_scrape_url", sa.String(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(
        op.f("ix_laptop_brands_name"), "laptop_brands", ["name"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema - drop laptop_brands table."""
    op.drop_index(op.f("ix_laptop_brands_name"), table_name="laptop_brands")
    op.drop_table("laptop_brands")
