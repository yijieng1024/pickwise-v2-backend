"""Drop laptop_customizations (step 10c (g))

Revision ID: a3c7e1f9b2d4
Revises: f2a7c4e9b1d3
Create Date: 2026-11-09

The Configure flow reads laptop_options; nothing reads this table. Its rows were
deleted first by app/scripts/retire_customizations_10c.py (2026-11-09, snapshots in
backups/), so the upgrade refuses while any row is left rather than drop data that
was never backed up. Downgrade recreates the table empty.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "a3c7e1f9b2d4"
down_revision = "f2a7c4e9b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM laptop_customizations")).scalar_one()
    if rows:
        raise RuntimeError(
            f"laptop_customizations still has {rows} rows. Run "
            "`python -m app.scripts.retire_customizations_10c --apply` first, which "
            "snapshots them to backups/, then migrate again."
        )
    op.drop_index(op.f("ix_laptop_customizations_laptop_id"), table_name="laptop_customizations")
    op.drop_table("laptop_customizations")


def downgrade() -> None:
    op.create_table(
        "laptop_customizations",
        sa.Column("option_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("price_add_rm", sa.Float(), nullable=False),
        sa.Column("dependency_note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("laptop_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["laptop_id"], ["laptops.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"],
                                name="fk_laptop_customizations_category_id_categories"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_laptop_customizations_laptop_id"), "laptop_customizations", ["laptop_id"], unique=False)
