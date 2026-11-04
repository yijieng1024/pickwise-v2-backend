"""laptops.weight_kg and laptops.fingerprint_reader may be null

Revision ID: e4b8d2f6a9c1
Revises: d1e5a9c3b7f2
Create Date: 2026-11-04

10a (decided 2026-11-04): Apple weight and Touch ID are read by code from the specs page,
and a failed parse writes null rather than 0.0 / False, which read as a measured weight
and a confirmed "no Touch ID". Dropping NOT NULL rewrites nothing. The downgrade puts
those placeholders back on any null row before restoring the constraint.
"""
from alembic import op
import sqlalchemy as sa

revision = "e4b8d2f6a9c1"
down_revision = "d1e5a9c3b7f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("laptops", "weight_kg", existing_type=sa.Float(), nullable=True)
    op.alter_column("laptops", "fingerprint_reader", existing_type=sa.Boolean(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE laptops SET weight_kg = 0.0 WHERE weight_kg IS NULL")
    op.execute("UPDATE laptops SET fingerprint_reader = false WHERE fingerprint_reader IS NULL")
    op.alter_column("laptops", "fingerprint_reader", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("laptops", "weight_kg", existing_type=sa.Float(), nullable=False)
