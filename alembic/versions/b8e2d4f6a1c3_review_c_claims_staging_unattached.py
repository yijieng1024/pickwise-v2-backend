"""Review C: chunk topic + one claim per window, unattached claims, staging

    laptop_review_chunks.topic          NULL on chunks written before Review C
    uq_review_chunk_window              one claim per (video_id, window start)
    review_unattached_claims            claims not attached to a linked laptop
    review_claims_staging               the next generation of a video's claims
    raw_youtube_reviews.reprocess_requested_at   links changed after processing

Production check before writing this (2026-10-23): 0 duplicate
(video_id, timestamp_start_seconds) pairs in laptop_review_chunks, so the
unique constraint applies without touching data.

Revision ID: b8e2d4f6a1c3
Revises: a7d3e5f19c42
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy

revision = "b8e2d4f6a1c3"
down_revision = "a7d3e5f19c42"
branch_labels = None
depends_on = None

_TOPICS = "('thermals', 'build', 'display', 'battery', 'performance', 'portability', 'price', 'other')"
_REASONS = "('other_laptop', 'several', 'none', 'config_unknown')"


def _claim_columns() -> list:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("channel_name", sa.String(), nullable=False),
        sa.Column("timestamp_start_seconds", sa.Integer(), nullable=False),
        sa.Column("timestamp_end_seconds", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.String(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=768), nullable=True),
        sa.Column("sentiment_tag", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("raw_youtube_reviews", sa.Column("reprocess_requested_at", sa.DateTime(), nullable=True))

    op.add_column("laptop_review_chunks", sa.Column("topic", sa.String(), nullable=True))
    op.create_check_constraint("ck_review_chunk_topic", "laptop_review_chunks", f"topic IN {_TOPICS}")
    op.create_unique_constraint(
        "uq_review_chunk_window", "laptop_review_chunks", ["video_id", "timestamp_start_seconds"]
    )

    op.create_table(
        "review_unattached_claims",
        *_claim_columns(),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("family_id", sa.Uuid(), sa.ForeignKey("laptop_family.id"), nullable=True),
        sa.Column("mentioned_laptop", sa.String(), nullable=True),
        sa.CheckConstraint(f"reason IN {_REASONS}", name="ck_unattached_reason"),
        sa.CheckConstraint(f"topic IN {_TOPICS}", name="ck_unattached_topic"),
        sa.UniqueConstraint("video_id", "timestamp_start_seconds", name="uq_unattached_window"),
    )
    op.create_index("ix_review_unattached_claims_video_id", "review_unattached_claims", ["video_id"])
    op.create_index("ix_review_unattached_claims_family_id", "review_unattached_claims", ["family_id"])

    op.create_table(
        "review_claims_staging",
        *_claim_columns(),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("family_id", sa.Uuid(), sa.ForeignKey("laptop_family.id"), nullable=True),
        sa.Column("laptop_id", sa.Uuid(), sa.ForeignKey("laptops.id"), nullable=True),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("mentioned_laptop", sa.String(), nullable=True),
        sa.CheckConstraint(
            "(reason IS NULL AND family_id IS NOT NULL AND scope IN ('family', 'config')"
            " AND (scope = 'family' OR laptop_id IS NOT NULL))"
            " OR (reason IS NOT NULL AND laptop_id IS NULL AND scope IS NULL)",
            name="ck_staging_kind",
        ),
        sa.CheckConstraint(f"reason IN {_REASONS}", name="ck_staging_reason"),
        sa.CheckConstraint(f"topic IN {_TOPICS}", name="ck_staging_topic"),
        sa.UniqueConstraint("video_id", "timestamp_start_seconds", name="uq_staging_window"),
    )
    op.create_index("ix_review_claims_staging_video_id", "review_claims_staging", ["video_id"])
    op.create_index("ix_review_claims_staging_family_id", "review_claims_staging", ["family_id"])
    op.create_index("ix_review_claims_staging_laptop_id", "review_claims_staging", ["laptop_id"])


def downgrade() -> None:
    # Loses data: staged and unattached claims and chunk topics have nowhere
    # to go in the old shape. Re-running Review C recreates them.
    op.drop_table("review_claims_staging")
    op.drop_table("review_unattached_claims")
    op.drop_constraint("uq_review_chunk_window", "laptop_review_chunks", type_="unique")
    op.drop_constraint("ck_review_chunk_topic", "laptop_review_chunks", type_="check")
    op.drop_column("laptop_review_chunks", "topic")
    op.drop_column("raw_youtube_reviews", "reprocess_requested_at")
