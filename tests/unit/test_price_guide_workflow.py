"""The workflow's promises, the write step's database check, and the Drive download.
The workflow holds the production DATABASE_URL, so what it can do is asserted, not
just described in a comment."""
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from ._adapters import db_target, fetch_guide_pdf, write_price_guide

host_of, project_ref, write_target = db_target.host_of, db_target.project_ref, db_target.write_target

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "price-guide.yml"
PRODUCTION = "abcdefghijklmnopqrst"
TEST_REF = "zyxwvutsrqponmlkjihg"
PROD_URL = f"postgresql://postgres.{PRODUCTION}:s3cret@aws-0-ap.pooler.supabase.com:5432/postgres"
TEST_URL = f"postgresql://postgres.{TEST_REF}:s3cret@aws-0-ap.pooler.supabase.com:5432/postgres"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow) -> dict[str, dict]:
    return {s["name"]: s for s in workflow["jobs"]["import"]["steps"] if "name" in s}


def test_the_workflow_only_runs_by_hand(workflow):
    # PyYAML reads the bare key `on` as True
    assert list(workflow[True]) == ["workflow_dispatch"]


def test_only_the_write_step_sees_the_database(workflow):
    holders = [name for name, step in _steps(workflow).items() if "DATABASE_URL" in step.get("env", {})]
    assert holders == ["Write to the staging tables"]
    assert "env" not in workflow and "env" not in workflow["jobs"]["import"]


def test_extraction_sees_no_secret(workflow):
    assert "secrets." not in json.dumps(_steps(workflow)["Extract the rows"])


def test_only_the_download_step_sees_the_service_account_key(workflow):
    holders = [n for n, s in _steps(workflow).items() if "GOOGLE_SERVICE_ACCOUNT_KEY" in json.dumps(s)]
    assert holders == ["Download the PDF"]


def test_workflow_inputs_never_reach_a_shell_command_directly(workflow):
    for name, step in _steps(workflow).items():
        assert "${{" not in step.get("run", ""), f"{name} interpolates into its script"


def test_the_write_step_targets_production_on_purpose(workflow):
    env = _steps(workflow)["Write to the staging tables"]["env"]
    assert env["PRICE_GUIDE_TARGET"] == "production"
    assert "PRODUCTION_DB_REF" in env


def test_production_needs_the_flag_and_the_right_project():
    env = {"PRICE_GUIDE_TARGET": "production", "DATABASE_URL": PROD_URL, "PRODUCTION_DB_REF": PRODUCTION}
    assert write_target("PRICE_GUIDE_TARGET", env) == PROD_URL


def test_with_no_flag_production_is_not_used():
    env = {"DATABASE_URL": PROD_URL, "TEST_DATABASE_URL": TEST_URL, "PRODUCTION_DB_REF": PRODUCTION}
    assert write_target("PRICE_GUIDE_TARGET", env) == TEST_URL


@pytest.mark.parametrize("env, message", [
    ({"PRICE_GUIDE_TARGET": "production", "DATABASE_URL": TEST_URL, "PRODUCTION_DB_REF": PRODUCTION}, "not the production project"),
    ({"PRICE_GUIDE_TARGET": "production", "PRODUCTION_DB_REF": PRODUCTION}, "DATABASE_URL is unset"),
    ({"PRICE_GUIDE_TARGET": "production", "DATABASE_URL": PROD_URL}, "PRODUCTION_DB_REF is not set"),
    ({"TEST_DATABASE_URL": TEST_URL}, "PRODUCTION_DB_REF is not set"),
    ({"TEST_DATABASE_URL": PROD_URL, "PRODUCTION_DB_REF": PRODUCTION}, "points at the production project"),
    ({"DATABASE_URL": PROD_URL, "PRODUCTION_DB_REF": PRODUCTION}, "No TEST_DATABASE_URL"),
])
def test_a_wrong_database_is_refused(env, message):
    with pytest.raises(RuntimeError, match=message):
        write_target("PRICE_GUIDE_TARGET", env)


def test_a_refusal_never_prints_the_url():
    env = {"PRICE_GUIDE_TARGET": "production", "DATABASE_URL": TEST_URL, "PRODUCTION_DB_REF": PRODUCTION}
    with pytest.raises(RuntimeError) as refused:
        write_target("PRICE_GUIDE_TARGET", env)
    assert "s3cret" not in str(refused.value) and TEST_REF not in str(refused.value)


def test_only_the_host_is_logged():
    assert host_of(PROD_URL) == "aws-0-ap.pooler.supabase.com"
    assert project_ref(PROD_URL) == PRODUCTION  # the ref is in the username, not the host


def test_the_write_script_runs_its_check_before_it_connects(monkeypatch, tmp_path, capsys):
    rows = tmp_path / "rows.json"
    rows.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["write_price_guide", str(rows)])
    monkeypatch.setenv("TEST_DATABASE_URL", PROD_URL)
    monkeypatch.setenv("PRODUCTION_DB_REF", PRODUCTION)
    monkeypatch.delenv("PRICE_GUIDE_TARGET", raising=False)
    monkeypatch.setattr(write_price_guide, "create_engine", lambda url: pytest.fail("connected"))

    with pytest.raises(RuntimeError, match="points at the production project"):
        write_price_guide.main()


class _Response:
    def __init__(self, *, meta=None, body=b""):
        self.meta, self.body = meta, body

    def raise_for_status(self):
        pass

    def json(self):
        return self.meta

    def iter_content(self, _size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Drive:
    def __init__(self, body=b"%PDF-1.7 guide", **meta):
        self.body = body
        self.meta = {
            "name": "guide.pdf", "mimeType": "application/pdf", "size": str(len(body)),
            "md5Checksum": hashlib.md5(body).hexdigest(), **meta,
        }

    def get(self, _url, params=None, stream=False):
        return _Response(body=self.body) if (params or {}).get("alt") == "media" else _Response(meta=self.meta)


def test_a_download_is_saved_under_drives_name(tmp_path):
    path = fetch_guide_pdf.download(_Drive(), "id", tmp_path)

    assert path == tmp_path / "guide.pdf"
    assert path.read_bytes() == b"%PDF-1.7 guide"


def test_a_download_that_does_not_match_drive_is_deleted(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        fetch_guide_pdf.download(_Drive(md5Checksum="0" * 32), "id", tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a PDF"):
        fetch_guide_pdf.download(_Drive(mimeType="text/plain"), "id", tmp_path)


def test_a_name_from_drive_cannot_leave_the_folder(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()

    path = fetch_guide_pdf.download(_Drive(name="../../escape.pdf"), "id", inner)

    assert path.parent == inner
