"""add raw_product_htmls table

Stores admin-uploaded source HTML for pages that cannot be fetched
automatically (Acer's store refuses automated requests). Product-agnostic by
design — see app/scraper/raw_html_model.py.

`target_id` intentionally has no foreign key: the target table is chosen by
product_type (laptop_scrape_urls today), and a real FK can only point at one
table. Integrity is enforced in app/scraper/html_ingest.py.

`scrape_status` on laptop_scrape_urls gains two lifecycle values
('html_uploaded', 'parsed'). It is a plain VARCHAR, not a native enum, so no
type change is required — existing rows keep their values.

Revision ID: b7e4a2f16c93
Revises: a3f9c1d7e224
Create Date: 2026-08-05

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b7e4a2f16c93'
down_revision: Union[str, Sequence[str], None] = 'a3f9c1d7e224'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'raw_product_htmls',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('product_type_id', sa.Uuid(), nullable=False),
        sa.Column('brand_id', sa.Uuid(), nullable=False),
        sa.Column('target_id', sa.Uuid(), nullable=False),
        sa.Column('canonical_url', sa.String(), nullable=False),
        sa.Column('raw_html', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['brand_id'], ['laptop_brands.id'], ),
        sa.ForeignKeyConstraint(['product_type_id'], ['product_types.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_raw_product_htmls_product_type_id'),
        'raw_product_htmls', ['product_type_id'], unique=False,
    )
    op.create_index(
        op.f('ix_raw_product_htmls_brand_id'),
        'raw_product_htmls', ['brand_id'], unique=False,
    )
    op.create_index(
        op.f('ix_raw_product_htmls_target_id'),
        'raw_product_htmls', ['target_id'], unique=False,
    )
    # Unique — the canonical URL identifies the product globally and is the
    # upsert key for re-uploads.
    op.create_index(
        op.f('ix_raw_product_htmls_canonical_url'),
        'raw_product_htmls', ['canonical_url'], unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_raw_product_htmls_canonical_url'), table_name='raw_product_htmls'
    )
    op.drop_index(
        op.f('ix_raw_product_htmls_target_id'), table_name='raw_product_htmls'
    )
    op.drop_index(
        op.f('ix_raw_product_htmls_brand_id'), table_name='raw_product_htmls'
    )
    op.drop_index(
        op.f('ix_raw_product_htmls_product_type_id'), table_name='raw_product_htmls'
    )
    op.drop_table('raw_product_htmls')
