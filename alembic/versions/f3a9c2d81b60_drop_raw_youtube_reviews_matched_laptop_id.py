"""drop raw_youtube_reviews.matched_laptop_id (Review A2)

review_laptop_link is the single source of truth for which laptop a review
covers (ADR-0012). Since Review A1 (7a3eafa) nothing reads or writes this
column; the drop must ship in the same deploy as the code that stopped
declaring it on the model.

match_confidence is KEPT: ingest still writes the matcher's score on every
row, and /reviews/raw and /reviews/pending still return it.

Backup taken before the drop (read-only export of id, video_id,
matched_laptop_id, match_confidence for all 93 production rows):
pickwise-backups/raw_youtube_reviews_matched_laptop_id_2026-10-22.json

Revision ID: f3a9c2d81b60
Revises: c5e9a1d7f3b2
Create Date: 2026-10-22
"""
from alembic import op
import sqlalchemy as sa

revision = "f3a9c2d81b60"
down_revision = "c5e9a1d7f3b2"
branch_labels = None
depends_on = None

_FK = "raw_youtube_reviews_matched_laptop_id_fkey"


def upgrade() -> None:
    # Dropping the column drops its FK with it; named explicitly anyway so the
    # migration states exactly what it removes.
    op.drop_constraint(_FK, "raw_youtube_reviews", type_="foreignkey")
    op.drop_column("raw_youtube_reviews", "matched_laptop_id")


def downgrade() -> None:
    # Restores the SHAPE only. The data cannot come back from this migration:
    # every row gets NULL. To restore the values, load them from the JSON
    # backup named in the module docstring.
    op.add_column(
        "raw_youtube_reviews",
        sa.Column("matched_laptop_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        _FK, "raw_youtube_reviews", "laptops", ["matched_laptop_id"], ["id"],
    )
