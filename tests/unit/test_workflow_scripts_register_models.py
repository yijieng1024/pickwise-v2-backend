"""Every script in app/scripts/ registers every table before it writes.

Run on its own (`python -m app.scripts...` in Actions), a script sees only the
models it imported; the first flush then fails resolving a foreign key to a
table nobody imported (`background_jobs.created_by` -> `users`). The app and the
integration tier never notice, because app.main imports every model first. So
this imports each script in a fresh process, with nothing else from the app.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WORKFLOW_SCRIPTS = sorted({
    m for wf in (ROOT / ".github" / "workflows").glob("*.yml")
    for m in re.findall(r"python -m (app\.scripts\.\w+)", wf.read_text(encoding="utf-8"))
})
# Every script, not only the workflows': any of them may be run on its own.
SCRIPTS = sorted(f"app.scripts.{p.stem}" for p in (ROOT / "app" / "scripts").glob("*.py") if p.stem != "__init__")

PROBE = """
import importlib, sys
importlib.import_module(sys.argv[1])
from sqlmodel import SQLModel
bad = [f"{t.name}.{fk.parent.name}" for t in SQLModel.metadata.tables.values()
       for fk in t.foreign_keys if fk._resolve_column(raiseerr=False) is None]
print(bad)
sys.exit(1 if bad else 0)
"""


def test_the_workflows_run_scripts():
    assert "app.scripts.weekly_pipeline" in WORKFLOW_SCRIPTS and "app.scripts.write_price_guide" in WORKFLOW_SCRIPTS
    assert set(WORKFLOW_SCRIPTS) <= set(SCRIPTS)


@pytest.mark.parametrize("module", SCRIPTS)
def test_a_script_resolves_every_foreign_key_on_its_own(module):
    done = subprocess.run([sys.executable, "-c", PROBE, module], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, f"{module}: unresolved foreign keys {done.stdout.strip()} {done.stderr[-500:]}"
