"""enable pgvector extension

The first revision in the chain, ahead of init_all_tables.

453fffc97e7b creates `laptop_embeddings.embedding` as VECTOR(768), so the
extension has to exist before it runs. Nothing in the history created it:
`alembic upgrade head` on a genuinely fresh database failed with
`type "vector" does not exist`. The only CREATE EXTENSION in the codebase sits
in app/database.py::init_db(), which (a) nothing calls and (b) would run after
the migrations anyway, since the container start command is
`alembic upgrade head && uvicorn`. Production has only ever worked because the
extension was already present on that project.

WHY INSERTED AT THE BASE RATHER THAN APPENDED AT HEAD. A revision at head is
reached only after init_all_tables has already failed, so it would not fix the
from-empty case at all -- which is the only case that is broken. Inserting
ahead of the base is safe for databases that already exist: alembic stores a
single version_num, and a database sitting at head is at a descendant of this
revision, so `upgrade head` is a no-op there and this never re-runs.

`IF NOT EXISTS` because every existing database already has the extension, and
because a hosted Postgres may have it enabled by the provider.

DOWNGRADE IS DELIBERATELY A NO-OP. An extension is not this schema's property:
it is database-wide, another schema may depend on it, and on a managed provider
it may predate this application entirely. Dropping something on the way down
that we merely ensured on the way up would be destructive beyond the scope of
the revision. The upgrade is idempotent, so a re-upgrade after a downgrade is
unaffected.

Revision ID: 0a1b2c3d4e5f
Revises:
Create Date: 2026-09-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0a1b2c3d4e5f'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # See the module docstring: intentionally empty.
    pass
