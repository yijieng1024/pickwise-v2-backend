"""questionnaire_questions.admin_note: internal notes, separate from help_text

Revision ID: e6a2c9f4b8d1
Revises: b4d8f2a6c1e3
Create Date: 2026-11-09

Additive, nullable. The seed migration put admin notes ("To know user's budget range
as reference") in help_text, which the wizard shows to users. Moving those six rows
is data, done by app/scripts/questionnaire_help_text.py, not here.
"""
from alembic import op
import sqlalchemy as sa

revision = "e6a2c9f4b8d1"
down_revision = "b4d8f2a6c1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("questionnaire_questions", sa.Column("admin_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("questionnaire_questions", "admin_note")
