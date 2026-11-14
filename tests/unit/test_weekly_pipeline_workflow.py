"""The weekly pipeline workflow's promises: it holds the production DATABASE_URL."""
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "weekly-pipeline.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_daily_at_three_in_malaysia_or_by_hand_never_on_a_pull_request():
    on = _workflow()[True]  # PyYAML reads the bare key `on` as True
    assert set(on) == {"schedule", "workflow_dispatch"}
    assert on["schedule"] == [{"cron": "0 19 * * *"}]


def test_runs_never_overlap_and_stop_after_three_hours():
    job = _workflow()["jobs"]["run"]
    assert job["timeout-minutes"] == 180
    assert job["concurrency"] == {"group": "weekly-pipeline", "cancel-in-progress": False}


def test_the_schedule_passes_scheduled_and_a_manual_run_does_not():
    [step] = [s for s in _workflow()["jobs"]["run"]["steps"] if s.get("name") == "Run the pipeline"]
    assert step["env"]["WEEKLY_PIPELINE_TARGET"] == "production"
    assert "${{" not in step["run"]
    assert "--apply --scheduled" in step["run"]
    assert step["run"].count("--apply") == 2
