"""
The throwaway Postgres the integration tier runs against.

WHY THIS TIER EXISTS AT ALL, ahead of the other unwritten ones: migration
a3f7d21c6b84 was applied straight to the live Supabase database, because that is
what `alembic upgrade head` targets when DATABASE_URL points there. It was
additive and harmless. The habit is not — the next migration that does an ALTER
COLUMN or a backfill takes the same path with real consequences. This container
is the database migrations get exercised against first.

Resolution order:
  1. TEST_DATABASE_URL, if set — the docker-compose.test.yml path, and what the
     Makefile target uses.
  2. testcontainers, which starts and removes pgvector/pgvector itself.
  3. Skip the whole tier with a message saying how to get one.

Never the third silently: a tier that passes because it ran nothing is worse
than one that is red, since it reports the same green as a real run.
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

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


def _production_urls() -> set[str]:
    """Every URL this tier must refuse to touch."""
    urls = set()
    for name in ("DATABASE_URL",):
        value = os.environ.get(name)
        if value:
            urls.add(value)
    try:
        from app.config import settings

        if getattr(settings, "database_url", None):
            urls.add(settings.database_url)
    except Exception:  # pragma: no cover - config is optional for this guard
        pass
    return urls


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return url

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
    yield url
    container.stop()
    return


@pytest.fixture(scope="session")
def engine(database_url):
    """
    A guard, not a convention: refuse to run against the production URL.

    Everything below drops and recreates the schema. A mis-set TEST_DATABASE_URL
    would otherwise do that to Supabase, and the whole reason this tier was
    promoted is that the two are one environment variable apart.
    """
    for production in _production_urls():
        assert database_url != production, (
            "the integration tier is pointed at the production database; "
            "it drops and recreates the schema and must never run there"
        )
    assert "supabase" not in database_url, "refusing to run against Supabase"

    eng = create_engine(database_url)
    with eng.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    return eng


@pytest.fixture
def clean_schema(engine):
    """Empty database, tables created from the models.

    Model-created rather than migration-created on purpose: a test that wants
    the migration path says so (see test_migrations.py), and the rest should not
    silently depend on migrations having been run.
    """
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield engine
    SQLModel.metadata.drop_all(engine)


@pytest.fixture
def session(clean_schema):
    with Session(clean_schema) as s:
        yield s


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
