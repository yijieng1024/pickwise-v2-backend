"""convert brand string to brand_id UUID

Revision ID: f3d8e2c1b5a9
Revises: e1a2b3c4d5e6
Create Date: 2026-06-10 12:00:00.000000

"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "f3d8e2c1b5a9"
down_revision: Union[str, Sequence[str], None] = "e1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - convert brand string to brand_id UUID."""
    # For laptop_scrape_urls table
    op.add_column("laptop_scrape_urls", sa.Column("brand_id", sa.Uuid(), nullable=True))

    # Populate brand_id from brand string by looking up in laptop_brands
    # This assumes brand names match between old string and new table
    op.execute("""
        UPDATE laptop_scrape_urls
        SET brand_id = lb.id
        FROM laptop_brands lb
        WHERE laptop_scrape_urls.brand = lb.name
    """)

    # Add foreign key constraint
    op.create_foreign_key(
        "fk_laptop_scrape_urls_brand_id",
        "laptop_scrape_urls",
        "laptop_brands",
        ["brand_id"],
        ["id"],
    )

    # Drop old brand column
    op.drop_column("laptop_scrape_urls", "brand")

    # Make brand_id NOT NULL
    op.alter_column("laptop_scrape_urls", "brand_id", nullable=False)

    # For raw_scrap_laptops table - convert brand to brand_id
    op.add_column("raw_scrap_laptops", sa.Column("brand_id", sa.Uuid(), nullable=True))

    # Populate brand_id from brand string
    op.execute("""
        UPDATE raw_scrap_laptops
        SET brand_id = lb.id
        FROM laptop_brands lb
        WHERE raw_scrap_laptops.brand = lb.name
    """)

    # Add foreign key constraint
    op.create_foreign_key(
        "fk_raw_scrap_laptops_brand_id",
        "raw_scrap_laptops",
        "laptop_brands",
        ["brand_id"],
        ["id"],
    )

    # Drop old brand column
    op.drop_column("raw_scrap_laptops", "brand")

    # Make brand_id NOT NULL
    op.alter_column("raw_scrap_laptops", "brand_id", nullable=False)


def downgrade() -> None:
    """Downgrade schema - revert to brand string."""
    # For raw_scrap_laptops
    op.drop_constraint(
        "fk_raw_scrap_laptops_brand_id", "raw_scrap_laptops", type_="foreignkey"
    )
    op.add_column(
        "raw_scrap_laptops",
        sa.Column("brand", sa.String(), nullable=False, server_default=""),
    )
    op.drop_column("raw_scrap_laptops", "brand_id")

    # For laptop_scrape_urls
    op.drop_constraint(
        "fk_laptop_scrape_urls_brand_id", "laptop_scrape_urls", type_="foreignkey"
    )
    op.add_column(
        "laptop_scrape_urls",
        sa.Column("brand", sa.String(), nullable=False, server_default=""),
    )
    op.drop_column("laptop_scrape_urls", "brand_id")
