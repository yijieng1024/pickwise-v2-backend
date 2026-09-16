"""
The unit tier must be importable with no configuration at all.

THIS IS THE DETECTOR; the lazy construction in app/config.py, app/database.py
and app/embeddings/service.py is the defence. They are kept apart deliberately:
a test that passes because it shares a process with the thing it is testing is
the failure mode this project has now hit twice.

The property had never held. `_adapters` imported a set of strings from the
agent tool package, which reached app.database, which called create_engine at
module scope -- so collecting the unit tier demanded a DATABASE_URL. Nobody
noticed for months because a developer machine always has a .env on disk, the
same way production only ever worked because Supabase happened to provision
pgvector. Both times the environment covered for the code, and the fix in both
cases is to make the property hold by construction and then pin it.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Everything app/config.py declares as required, plus the optional keys, so an
# inherited value cannot quietly satisfy an import.
_SECRET_NAMES = (
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "SECRET_KEY",
    "GEMINI_API_KEY",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "YOUTUBE_API_KEY",
    "SERP_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "WEBSHARE_PROXY_USERNAME",
    "WEBSHARE_PROXY_PASSWORD",
    "PRODUCTION_DB_REF",
    "FRONTEND_URL",
    "BACKEND_URL",
)


# Neutralising .env is the whole difficulty, and getting it wrong makes this
# test pass for the wrong reason -- which it did on the first attempt, caught
# only by the mutation check.
#
# Scrubbing os.environ is not enough: app modules call load_dotenv(), and
# python-dotenv's find_dotenv() walks up from the CALLING MODULE's directory,
# not from the working directory. From app/database.py that reaches the repo
# root and puts every value straight back. Changing cwd does not help for the
# same reason.
#
# So dotenv is stubbed out inside the subprocess BEFORE any app module is
# imported, and the process runs from a directory outside the repository, which
# also stops pydantic-settings' relative `env_file=".env"` from resolving.
# Nothing on disk is renamed or deleted -- a test that moves .env aside loses
# it for good if the process is killed between the move and the restore.
_DISARM_DOTENV = """
    import dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.find_dotenv = lambda *a, **k: ""
    import dotenv.main
    dotenv.main.load_dotenv = dotenv.load_dotenv
    dotenv.main.find_dotenv = dotenv.find_dotenv
"""


def _run_scrubbed(code: str) -> subprocess.CompletedProcess:
    """
    Run `code` in a subprocess with every secret unset and .env unreachable.

    A SUBPROCESS because unsetting environment variables inside this process is
    far too late: by the time a test body runs, conftest and _adapters have long
    since imported everything. The question is what happens at IMPORT, so it has
    to be asked in a process that has not imported anything yet.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_NAMES}
    env["PYTHONPATH"] = str(REPO_ROOT)
    with tempfile.TemporaryDirectory() as outside_the_repo:
        return subprocess.run(
            # Dedented SEPARATELY: the two blocks are indented to different
            # depths in this file, and dedenting the concatenation strips only
            # the common prefix, leaving one of them over-indented.
            [
                sys.executable,
                "-c",
                textwrap.dedent(_DISARM_DOTENV) + textwrap.dedent(code),
            ],
            capture_output=True,
            text=True,
            cwd=outside_the_repo,
            env=env,
        )


def test_the_probe_actually_scrubs_the_environment():
    """
    The detector needs its own detector. If .env leaks into the subprocess,
    every assertion below passes vacuously -- which is exactly what happened on
    the first version of this file, where load_dotenv() found the repo's .env
    by walking up from app/database.py.
    """
    result = _run_scrubbed("""
        import os
        import dotenv
        dotenv.load_dotenv()
        print("DATABASE_URL:", os.getenv("DATABASE_URL"))
        print("GEMINI_API_KEY:", os.getenv("GEMINI_API_KEY"))
    """)
    assert "DATABASE_URL: None" in result.stdout, result.stdout
    assert "GEMINI_API_KEY: None" in result.stdout, result.stdout


_IMPORT_PROBE = """
    import importlib
    import sys
    import traceback

    # The single entry point every unit test file goes through. Importing it
    # pulls in exactly the production surface the tier depends on, so the list
    # is DERIVED rather than hardcoded -- a new unit test file that imports a
    # new adapter symbol is covered the day it is written, with no edit here.
    #
    # What this does NOT cover: a unit test that imports from app.* directly,
    # bypassing the adapter. The suite's own rule forbids that (_adapters is
    # "THE ONLY FILE THAT TOUCHES PRODUCTION IMPORTS"), and
    # test_no_unit_test_imports_app_directly below enforces it.
    try:
        importlib.import_module("__MODULE__")
    except BaseException:
        # The CHAIN is the diagnosis, not the exception. The original finding
        # was actionable only because the path was visible:
        #   _adapters -> agent.tools -> laptop_tools -> database
        frames = traceback.extract_tb(sys.exc_info()[2])
        chain = [f"{f.filename}:{f.lineno}" for f in frames]
        print("IMPORT-CHAIN-START")
        for step in chain:
            print(step)
        print("IMPORT-CHAIN-END")
        traceback.print_exc()
        sys.exit(1)
    print("OK")
"""


# app.main is here for the integration and migrations CI jobs, whose shared
# conftest imports it: that import built Settings() through the CORS middleware,
# so both jobs failed at COLLECTION for want of five secrets they never use.
# Same property, same detector -- a second probe would be a second place for
# the dotenv disarming to go subtly wrong.
@pytest.mark.parametrize("module", ["tests.unit._adapters", "app.main"])
def test_imports_with_no_configuration(module):
    """
    Catches the reintroduction of module-scope construction that needs a
    secret: an engine, a settings object, an API client. All three existed and
    all three are now built on first use.

    The failure message is the import chain, because "import failed" is not a
    diagnosis and the chain is.
    """
    result = _run_scrubbed(_IMPORT_PROBE.replace("__MODULE__", module))

    if result.returncode != 0:
        chain = ""
        if "IMPORT-CHAIN-START" in result.stdout:
            chain = result.stdout.split("IMPORT-CHAIN-START")[1].split("IMPORT-CHAIN-END")[0]
        pytest.fail(
            f"{module} cannot be imported without configuration.\n\n"
            "IMPORT CHAIN (innermost last):\n"
            + "\n".join(f"    {line}" for line in chain.strip().splitlines())
            + "\n\nSomething on that chain builds an object at MODULE SCOPE that "
            "needs a secret. Make it lazy where it is constructed -- do not add "
            "the secret to the unit job, which would restore the illusion the "
            "tier had for months.\n\nstderr:\n" + result.stderr[-2000:],
            pytrace=False,
        )


def test_no_unit_test_imports_app_directly():
    """
    The derivation above is only complete while _adapters really is the single
    door. A unit test importing app.* directly would slip past the probe, so
    the rule is asserted rather than trusted.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "tests" / "unit").glob("test_*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import app", "from app")):
                offenders.append(f"{path.name}:{number}: {stripped}")

    assert not offenders, (
        "unit tests must reach production through tests/unit/_adapters.py, which "
        "is the only file allowed to import app.* -- otherwise the no-secrets "
        "probe cannot see what they pull in:\n    " + "\n    ".join(offenders)
    )
