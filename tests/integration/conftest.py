"""
The throwaway Postgres the integration tier runs against.

WHY THIS TIER EXISTS AT ALL, ahead of the other unwritten ones: migration
a3f7d21c6b84 was applied straight to the live Supabase database, because that is
what `alembic upgrade head` targets when DATABASE_URL points there. It was
additive and harmless. The habit is not — the next migration that does an ALTER
COLUMN or a backfill takes the same path with real consequences. This container
is the database migrations get exercised against first.

Resolution order:
  1. TEST_DATABASE_URL, if set — a dedicated Supabase project, or the
     docker-compose.test.yml container.
  2. testcontainers, which starts and removes pgvector/pgvector itself.
  3. Skip the whole tier with a message saying how to get one.

Never the third silently: a tier that passes because it ran nothing is worse
than one that is red, since it reports the same green as a real run.

When the target is a hosted project rather than a container, two things change
and both are handled below: the production guard has to compare project refs
rather than hostnames (assert_not_production), and tests have to clean up after
themselves rather than relying on the database being thrown away (the `session`
fixture rolls back).
"""

import os
import re
import uuid

import pytest
from dotenv import load_dotenv
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

load_dotenv(".env")

# Importing any model module registers every table on SQLModel.metadata via the
# bottom-of-module deferred imports; alembic/env.py relies on the same thing.
from app.laptops.laptop_models import Laptop, LaptopStatus  # noqa: F401
from app.laptops.brand_model import LaptopBrand  # noqa: F401
from app.laptops.family_model import LaptopFamily  # noqa: F401

_SKIP_REASON = (
    "No test database. Either start one:\n"
    "    docker compose -f docker-compose.test.yml up -d\n"
    "    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:55432/pickwise_test\n"
    "or install Docker so testcontainers can start pgvector/pgvector itself.\n"
    "This tier is skipped rather than passed on purpose — it has run nothing."
)


# Supabase project ref, from either connection style:
#   direct  postgresql://postgres:pw@db.<ref>.supabase.co:5432/postgres
#   pooler  postgresql://postgres.<ref>:pw@aws-0-<region>.pooler.supabase.com:5432/postgres
# The pooler puts the ref in the USERNAME, not the host, so one pattern is not
# enough and matching on the host alone silently fails on the pooler URL.
_DIRECT_REF = re.compile(r"(?:^|@|\.)db\.([a-z0-9]{16,})\.supabase\.co", re.I)
_POOLER_REF = re.compile(r"//postgres\.([a-z0-9]{16,})[:@]", re.I)


def project_ref(url: str) -> str | None:
    """The Supabase project ref in `url`, or None if it is not a Supabase URL."""
    if not url:
        return None
    for pattern in (_POOLER_REF, _DIRECT_REF):
        match = pattern.search(url)
        if match:
            return match.group(1).lower()
    return None


def assert_not_production(url: str) -> None:
    """
    Refuse to run against the production database.

    This guard is stricter than the one written for the Docker plan, and the
    reason is specific: the test database and production are BOTH Supabase, so
    their URLs are the same shape and differ only by the project ref. Every
    cheap check — "contains supabase", "is not localhost", a hostname pattern —
    passes on production. Only the ref distinguishes them, and everything below
    drops and recreates the schema.

    PRODUCTION_DB_REF must be set. A guard with nothing to compare against that
    shrugs and continues is worse than no guard, because it reads as protection
    in the report while protecting nothing. Unset is a hard failure.
    """
    expected = (os.environ.get("PRODUCTION_DB_REF") or "").strip().lower()
    if not expected:
        raise RuntimeError(
            "PRODUCTION_DB_REF is not set. The integration tier drops and "
            "recreates the schema, and the test and production databases are "
            "both Supabase URLs differing only by project ref — without the "
            "production ref there is nothing to compare against, so the tier "
            "refuses to start rather than guess."
        )

    found = project_ref(url)
    if found is None:
        # Not a Supabase URL at all (a local container, say). Nothing to
        # confuse with production; the equality check below still applies.
        pass
    elif found == expected:
        raise RuntimeError(
            f"TEST_DATABASE_URL points at the PRODUCTION project ref {found!r} "
            f"(PRODUCTION_DB_REF={expected!r}). This tier drops and recreates "
            "every table. Refusing to start."
        )

    production_url = os.environ.get("DATABASE_URL")
    if production_url and url.strip() == production_url.strip():
        raise RuntimeError(
            "TEST_DATABASE_URL is byte-identical to DATABASE_URL. Refusing to start."
        )


@pytest.fixture(scope="session")
def database_url():
    """A generator fixture throughout.

    This function contains a `yield`, so pytest treats it as a generator
    fixture and an early `return url` yields nothing — every test then errors
    with "database_url did not yield a value" rather than running. Both exits
    yield.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        # Checked before anything connects: the caller may have pointed this at
        # production, and the next fixture drops every table.
        assert_not_production(url)
        yield url
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip(_SKIP_REASON, allow_module_level=True)

    try:
        container = PostgresContainer("pgvector/pgvector:pg16")
        container.start()
    except Exception as exc:  # Docker not installed, daemon not running, no image
        pytest.skip(f"{_SKIP_REASON}\n\nUnderlying error: {exc}", allow_module_level=True)

    url = container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    try:
        yield url
    finally:
        container.stop()


@pytest.fixture(scope="session")
def engine(database_url):
    """
    A guard, not a convention: see assert_not_production above. Everything
    below drops and recreates the schema, and the test and production databases
    are one project ref apart.
    """
    assert_not_production(database_url)

    eng = create_engine(database_url)
    try:
        with eng.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
    except Exception as exc:
        # Configured but unreachable -- a wrong password, a paused project, no
        # route. Skipped rather than errored 21 times with the same traceback,
        # but the reason is carried verbatim so this can never be mistaken for
        # "no database configured": that one says so in its own words.
        pytest.skip(
            "TEST_DATABASE_URL is set but the database could not be reached, so this tier has run NOTHING. "
            f"target={database_url.split('@')[-1]} "
            f"error={type(exc).__name__}: {str(exc)[:300]}",
            allow_module_level=True,
        )
    return eng


@pytest.fixture(scope="session")
def clean_schema(engine):
    """
    The schema, built ONCE for the whole session.

    This used to drop and recreate every table per test, which is fine against a
    container you throw away and wrong against a hosted database: ~25 DDL
    statements per test over the network, and any interruption leaves the
    project schema-less rather than merely dirty.

    Model-created rather than migration-created on purpose — a test that wants
    the migration path says so (see test_migrations.py), and the rest should not
    silently depend on migrations having been run.
    """
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture
def session(clean_schema):
    """
    One transaction per test, rolled back at the end. Nothing is ever committed
    to the database.

    A container is discarded; this project persists, so a test that leaves rows
    behind poisons every later run — and the data-invariant tests are the ones
    that would fail, which makes leftovers look exactly like a production bug.

    The mechanism: an outer transaction is opened on a single connection, the
    Session joins that connection rather than opening its own, and the outer
    transaction is rolled back afterwards. session.commit() inside a test
    therefore commits only to the SAVEPOINT the Session runs in — the code under
    test sees its writes, the database never keeps them.
    """
    connection = clean_schema.connect()
    transaction = connection.begin()
    s = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def brand(session) -> LaptopBrand:
    row = LaptopBrand(name="Asus", base_scrape_url="https://example.invalid/asus")
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def make_laptop(brand_id: uuid.UUID, **overrides) -> Laptop:
    """One row with every NOT NULL column filled. Overrides are what the test
    is actually about; everything else is noise it should not have to state."""
    fields = dict(
        brand_id=brand_id,
        model_code=f"TEST-{uuid.uuid4().hex[:8]}",
        product_name="Test Laptop",
        price_rm=4000.0,
        status=LaptopStatus.ACTIVE.value,
        processor_model="Intel Core i7-14650HX",
        gpu_model="NVIDIA GeForce RTX 5060 Laptop GPU",
        ram_gb=16,
        ssd_gb=512,
        display_size_inch=16.0,
        weight_kg=2.2,
        battery_wh=90.0,
    )
    fields.update(overrides)
    return Laptop(**fields)


@pytest.fixture
def active_and_suspended(session, brand):
    """The pair every status-filter test needs: one recommendable row and one
    retired one, identical in every other respect."""
    active = make_laptop(brand.id, product_name="Visible Laptop", status="active")
    suspended = make_laptop(brand.id, product_name="Retired Laptop", status="suspended")
    session.add(active)
    session.add(suspended)
    session.commit()
    session.refresh(active)
    session.refresh(suspended)
    return active, suspended
