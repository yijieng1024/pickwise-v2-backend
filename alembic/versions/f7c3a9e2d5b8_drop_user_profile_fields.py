"""Drop users.birthday, users.gender and users.occupation

Revision ID: f7c3a9e2d5b8
Revises: e6a2c9f4b8d1
Create Date: 2026-11-09

Collected on the profile and read by nothing; occupation is replaced by
user_background.user_type (decided 2026-11-09). The values were emptied first by
app/scripts/retire_user_profile_fields.py (snapshots in backups/), so the upgrade
refuses while any value is left rather than drop data that was never backed up.
Downgrade re-adds the columns empty.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "f7c3a9e2d5b8"
down_revision = "e6a2c9f4b8d1"
branch_labels = None
depends_on = None

COLUMNS = ("birthday", "gender", "occupation")


def upgrade() -> None:
    left = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM users WHERE birthday IS NOT NULL OR gender IS NOT NULL OR occupation IS NOT NULL"
    )).scalar_one()
    if left:
        raise RuntimeError(
            f"{left} users still have a birthday, gender or occupation. Run "
            "`python -m app.scripts.retire_user_profile_fields --apply` first, which "
            "snapshots them to backups/, then migrate again."
        )
    for column in COLUMNS:
        op.drop_column("users", column)


def downgrade() -> None:
    op.add_column("users", sa.Column("birthday", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("gender", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column("users", sa.Column("occupation", sqlmodel.sql.sqltypes.AutoString(), nullable=True))
