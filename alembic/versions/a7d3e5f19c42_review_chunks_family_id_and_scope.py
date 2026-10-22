"""laptop_review_chunks: family_id + scope, laptop_id nullable (Review B1)

ADR-0012 section 2. A chunk now records the product line it belongs to and
who its claim applies to:

    family_id  NOT NULL  -- backfilled from the chunk's laptop
    scope      'config' | 'family'
    laptop_id  NULL      -- for family-scope chunks from a family-only link

Existing chunks all came from a config-level link, so they backfill to
scope = 'config'. Production check before writing this: 346 chunks, 0 whose
laptop has no family -- the SET NOT NULL below fails loudly if that changes.

Revision ID: a7d3e5f19c42
Revises: f3a9c2d81b60
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "a7d3e5f19c42"
down_revision = "f3a9c2d81b60"
branch_labels = None
depends_on = None

_T = "laptop_review_chunks"


def upgrade() -> None:
    op.add_column(_T, sa.Column("family_id", sa.Uuid(), nullable=True))
    op.add_column(_T, sa.Column("scope", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.execute(
        "UPDATE laptop_review_chunks c SET family_id = l.family_id "
        "FROM laptops l WHERE l.id = c.laptop_id"
    )
    op.execute("UPDATE laptop_review_chunks SET scope = 'config'")
    op.alter_column(_T, "family_id", nullable=False)
    op.alter_column(_T, "scope", nullable=False)
    op.create_foreign_key(
        "laptop_review_chunks_family_id_fkey", _T, "laptop_family", ["family_id"], ["id"],
    )
    op.create_index("ix_laptop_review_chunks_family_id", _T, ["family_id"])
    op.alter_column(_T, "laptop_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint("ck_review_chunk_scope", _T, "scope IN ('family', 'config')")
    op.create_check_constraint(
        "ck_review_chunk_config_has_laptop", _T, "scope = 'family' OR laptop_id IS NOT NULL",
    )


def downgrade() -> None:
    # Loses data: family-scope chunks with no laptop cannot exist in the old
    # shape, so they are deleted. Re-processing those reviews after upgrading
    # again recreates them (it costs one Gemini call per window).
    op.execute("DELETE FROM laptop_review_chunks WHERE laptop_id IS NULL")
    op.drop_constraint("ck_review_chunk_config_has_laptop", _T, type_="check")
    op.drop_constraint("ck_review_chunk_scope", _T, type_="check")
    op.alter_column(_T, "laptop_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_index("ix_laptop_review_chunks_family_id", table_name=_T)
    op.drop_constraint("laptop_review_chunks_family_id_fkey", _T, type_="foreignkey")
    op.drop_column(_T, "scope")
    op.drop_column(_T, "family_id")
