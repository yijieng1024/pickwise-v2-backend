"""add laptop_family table and laptops.family_id

Revision ID: e2c76b019f4d
Revises: d9e1f4a86c27
Create Date: 2026-08-19

`family_key` is indexed but NOT unique: several seed keys legitimately map to
one family once an admin merges up to the product line ("14-inch macbook pro"
and "16-inch macbook pro" are both MacBook Pro), and a unique constraint would
make that merge impossible to express.

`laptops.family_id` is nullable with no default — null is the correct state for
"we don't know", and every laptop starts there. The grouping is populated by
POST /families/regroup (or `python -m app.scripts.backfill_families --apply`)
rather than by this migration: the seed grouping is a proposal a human has to
review, and baking a guess into the schema change would hide that.
"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e2c76b019f4d"
down_revision: Union[str, Sequence[str], None] = "d9e1f4a86c27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "laptop_family",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("family_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["laptop_brands.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_laptop_family_brand_id"), "laptop_family", ["brand_id"])
    op.create_index(op.f("ix_laptop_family_name"), "laptop_family", ["name"])
    op.create_index(op.f("ix_laptop_family_family_key"), "laptop_family", ["family_key"])
    op.create_index(op.f("ix_laptop_family_is_verified"), "laptop_family", ["is_verified"])

    op.add_column("laptops", sa.Column("family_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_laptops_family_id"), "laptops", ["family_id"])
    op.create_foreign_key(
        "fk_laptops_family_id_laptop_family", "laptops", "laptop_family",
        ["family_id"], ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_laptops_family_id_laptop_family", "laptops", type_="foreignkey")
    op.drop_index(op.f("ix_laptops_family_id"), table_name="laptops")
    op.drop_column("laptops", "family_id")

    op.drop_index(op.f("ix_laptop_family_is_verified"), table_name="laptop_family")
    op.drop_index(op.f("ix_laptop_family_family_key"), table_name="laptop_family")
    op.drop_index(op.f("ix_laptop_family_name"), table_name="laptop_family")
    op.drop_index(op.f("ix_laptop_family_brand_id"), table_name="laptop_family")
    op.drop_table("laptop_family")
