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

import contextlib
import os
import re
import uuid

import pytest
from dotenv import load_dotenv
from sqlalchemy import MetaData, text
from sqlmodel import Session, SQLModel, create_engine

load_dotenv(".env")

# Importing any model module registers every table on SQLModel.metadata via the
# bottom-of-module deferred imports; alembic/env.py relies on the same thing.
from app.laptops.laptop_models import Laptop, LaptopStatus  # noqa: F401
from app.laptops.brand_model import LaptopBrand  # noqa: F401
from app.laptops.family_model import LaptopFamily  # noqa: F401

# The WHOLE app, so every table any route can touch is on SQLModel.metadata
# before clean_schema builds it. create_all only creates what has been
# imported, and Tier 4 drives real routes that reach users, conversations,
# messages, agent_run_logs and more. Without this, a table is simply absent and
# the failure looks like a route bug ("relation users does not exist") rather
# than a missing import -- the same trap alembic/env.py documents for
# autogenerate. Safe to import eagerly now that settings, the engine and the
# embedder are built on first use.
import app.main  # noqa: E402,F401

_SKIP_REASON = (
    "No test database. Either start one:\n"
    "    docker compose -f docker-compose.test.yml up -d\n"
    "    TEST_DATABASE_URL=postgresql://postgres:${TEST_DB_PASSWORD:-postgres}@localhost:55432/pickwise_test\n"
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


@pytest.fixture(scope="session", autouse=True)
def app_settings(database_url):
    """
    The app's `settings`, for this tier: the test database URL and every default,
    and NOTHING else -- no secret, real or dummy.

    The app needs a settings object here even though no test uses a secret:
    Starlette builds the middleware stack on the first request and the CORS
    factory reads settings.cors_origins, and the lifespan test patches
    settings.database_url. Left to itself the proxy would build Settings(),
    which demands five secrets (so the CI job could not run) and, locally, reads
    .env -- whose DATABASE_URL is production.

    model_construct skips validation, so a secret field is simply ABSENT: a
    route that reaches Gemini or SMTP raises AttributeError instead of calling
    out with a real key. Dummy values would hide exactly that, and would need
    updating every time a required setting is added.
    """
    import app.config as config
    from app.config import Settings

    proxy = config.settings
    if object.__getattribute__(proxy, "_real") is not None:
        pytest.fail(
            "app.config.settings was resolved before the integration tier set it; "
            "it was built from the environment and .env, whose DATABASE_URL may be "
            "production. Find what read settings at import."
        )
    object.__setattr__(proxy, "_real", Settings.model_construct(database_url=database_url))
    try:
        yield proxy
    finally:
        object.__setattr__(proxy, "_real", None)


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


def drop_everything(engine) -> None:
    """
    Drop every table actually in the database, not every table the models know
    about.

    SQLModel.metadata.drop_all only knows the tables whose modules have been
    imported, and the migrations create a few this tier never imports. Dropping
    a partial set fails on the foreign keys the survivors still hold
    ("cannot drop table laptops because other objects depend on it"), which is
    how a test that had migrated the database poisoned the next one.

    Reflection asks the database what is there. DROP SCHEMA would be shorter and
    is deliberately not used: it would also take anything else living in this
    schema, which is not ours to assume.
    """
    reflected = MetaData()
    reflected.reflect(bind=engine)
    reflected.drop_all(bind=engine)


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
    drop_everything(engine)
    SQLModel.metadata.create_all(engine)
    yield engine
    drop_everything(engine)


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


# ---------------------------------------------------------------------------
# Tier 4 -- the API client
# ---------------------------------------------------------------------------


@pytest.fixture
def api_user(session):
    """A real, persisted, ACTIVE, non-admin account. Non-admin on purpose: the
    auth override below must not be able to grant a privilege the account does
    not have."""
    from app.users.models import User

    user = User(
        username=f"api-{uuid.uuid4().hex[:8]}",
        email=f"api-{uuid.uuid4().hex[:8]}@example.invalid",
        hashed_password="not-a-real-hash",
        status="active",
        role="user",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@contextlib.contextmanager
def _app_client(session, user, monkeypatch):
    """
    FastAPI's TestClient against the real app, inside the test's rollback
    transaction. Three things have to be redirected, and the third is the one
    that is easy to miss.

    1. `get_session` -> the test's session. Routes that take a session
       dependency then read and write inside the transaction that is rolled
       back at the end, so nothing persists.

    2. Authentication -> `api_user`. See WHAT THIS BYPASSES below.

    3. Every module-level `engine` -> the test's CONNECTION. This is not
       optional and not a detail. `session_scope()` -- used by the agent
       endpoints, which deliberately take no session dependency -- builds
       `Session(engine)` directly, and so does monitoring_service; `engine` is
       built from DATABASE_URL, which in a developer .env is PRODUCTION.
       TestClient runs the real app, so without this a test of /agent/chat
       would write its conversation into the live database.

       The swap is done by NAME in each module's namespace, not by pointing the
       lazy proxy at the connection. That was the first attempt, and it failed
       for a reason worth recording: SQLAlchemy decides how to use a bind with
       isinstance(bind, Connection), and a proxy is not a Connection, so
       Session treated it as an Engine and called .connect() on it. The
       modules are found by scanning sys.modules for any attribute that IS the
       proxy object, so a new `from app.database import engine` is covered
       without editing this fixture. The proxy itself is never resolved, which
       means no production engine is ever constructed during the tier.

    WHAT THIS BYPASSES, precisely:
      - JWT validation: signature, expiry, and presence of `sub`
      - the user-exists lookup
      - the `status != "active"` -> 403 re-check in _resolve_user
    WHAT IT DOES NOT BYPASS:
      - `get_current_admin`'s role check -- it depends on get_current_user, so
        it receives the non-admin `api_user` and still returns 403
      - per-resource ownership, e.g. service.get_conversation's user_id check
    Both are asserted in test_api_contract.py, because a fixture that quietly
    disabled more than authentication would let a broken authorization check
    pass. The one real gap: a suspended account's 403 cannot be tested through
    this override, since the override IS the account.

    The lifespan deliberately does NOT run (no `with TestClient(...)`): its job
    recovery writes to background_jobs, and nothing here should depend on it.
    test_a_bad_database_url_fails_startup_before_anything_runs enters it on
    purpose, with recovery stubbed.
    """
    import sys

    import app.database as database
    from fastapi.testclient import TestClient

    from app.database import get_session
    from app.main import app
    from app.users.auth import get_current_user, get_current_user_detached

    connection = session.connection()
    assert_not_production(str(connection.engine.url))

    proxy = database.engine
    if object.__getattribute__(proxy, "_real") is not None:
        # Something resolved the app's engine before this fixture ran. Whatever
        # it built, it built from DATABASE_URL -- refuse rather than run a tier
        # that may already have touched production.
        pytest.fail(
            "app.database.engine was resolved before Tier 4 redirected it; it was "
            "built from DATABASE_URL, which may be production. Find what resolved "
            "it and keep the integration tier from touching the app's engine."
        )

    swapped = []
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("app"):
            continue
        for name, value in list(vars(module).items()):
            if value is proxy:
                swapped.append((module, name))
                monkeypatch.setattr(module, name, connection)
    assert swapped, "found no module holding the app engine -- the redirect did nothing"

    def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_detached] = lambda: user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        # monkeypatch restores the module attributes itself.


@pytest.fixture
def api_client(session, api_user, monkeypatch):
    """A client authenticated as the NON-admin api_user. See _app_client."""
    with _app_client(session, api_user, monkeypatch) as client:
        yield client


@pytest.fixture
def admin_user(session):
    from app.users.models import User

    user = User(
        username=f"admin-{uuid.uuid4().hex[:8]}",
        email=f"admin-{uuid.uuid4().hex[:8]}@example.invalid",
        hashed_password="not-a-real-hash",
        status="active",
        role="admin",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture
def admin_client(session, admin_user, monkeypatch):
    """
    A client authenticated as an admin. A SEPARATE fixture rather than a role
    flag on api_client, so a test that needs admin rights has to ask for them by
    name -- and so the non-admin client stays the default, which is the one that
    can prove get_current_admin still enforces the role.

    Do not request both clients in one test: each redirects the app's engine,
    and the second would find nothing left to redirect.
    """
    with _app_client(session, admin_user, monkeypatch) as client:
        yield client
