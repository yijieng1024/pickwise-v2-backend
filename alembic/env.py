import os
from dotenv import load_dotenv
from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

from sqlmodel import SQLModel
# Every table model, or autogenerate emits a DROP TABLE for the live table it
# cannot see. The list lives in app/all_models.py, shared with the scripts.
import app.all_models  # noqa: E402,F401

load_dotenv()

config = context.config

# Which database a migration runs against, in order of preference.
#
# TEST_DATABASE_URL wins. Migration a3f7d21c6b84 was applied straight to the
# live Supabase database because DATABASE_URL is what this file read and
# DATABASE_URL is production -- `alembic upgrade head`, the most ordinary
# command in the project, reached production with no flag and no prompt. That
# one was additive and reversible; the next ALTER COLUMN or backfill would not
# be. Exercising a migration locally must be the default, and production must
# take a deliberate, differently-spelled command:
#
#   alembic upgrade head                          -> the test container
#   ALEMBIC_TARGET=production alembic upgrade head -> Supabase, on purpose
#
# See docker-compose.test.yml and tests/integration/test_migrations.py.
_TARGET = os.environ.get("ALEMBIC_TARGET", "").lower()
if _TARGET == "production":
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("ALEMBIC_TARGET=production but DATABASE_URL is unset")
    print(f"alembic: targeting PRODUCTION ({db_url.split('@')[-1]})")
else:
    db_url = os.environ.get("TEST_DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "No TEST_DATABASE_URL. Start the throwaway database first:\n"
            "    docker compose -f docker-compose.test.yml up -d\n"
            "    export TEST_DATABASE_URL="
            "postgresql://postgres:${TEST_DB_PASSWORD:-postgres}@localhost:55432/pickwise_test\n"
            "To migrate production on purpose, run with ALEMBIC_TARGET=production."
        )

if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
