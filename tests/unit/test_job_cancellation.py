"""
Cooperative cancellation of background jobs.

Jobs run in-process on a worker thread, so there is no safe way to kill one
mid-item — the alternative to letting the current item finish is a half-written
record. Cancellation is therefore a REQUEST (`cancelling`) that the worker
notices between items and turns into an OUTCOME (`cancelled`).

Three things have to hold for that to work, and each is a separate way to get
it wrong:

  1. `cancelling` must not be terminal. A UI that treats it as finished stops
     polling while a scrape is still mid-page, and reports the job as over
     while the server is still working.
  2. `cancelled` must be terminal, or `run_job`'s `finally` backstop overwrites
     it with `failed` — a deliberate stop reported as a crash.
  3. A cancelled job must NOT read 100%. The whole value of the number is
     showing how far it got; snapping it to 100 claims the remaining items ran.

None of this needs a database: the state machine is the thing under test.
"""

from tests.unit._adapters import (
    job_status,
    make_job,
    progress_seeing_status,
    read_job,
)

JobStatus = job_status()


# ---------------------------------------------------------------------------
# The status vocabulary
# ---------------------------------------------------------------------------


def test_cancelling_is_not_terminal():
    """The worker is still running. See failure mode 1 in the module docstring."""
    assert JobStatus.CANCELLING not in JobStatus.TERMINAL


def test_cancelled_is_terminal():
    """Otherwise run_job's backstop rewrites a clean stop as a crash."""
    assert JobStatus.CANCELLED in JobStatus.TERMINAL


def test_cancelling_counts_as_active():
    """
    ACTIVE is what the cancel endpoint accepts and what reset_stale_jobs
    sweeps. A job cancelled just before a deploy must still be recovered on
    boot rather than left in `cancelling` forever.
    """
    assert JobStatus.CANCELLING in JobStatus.ACTIVE
    assert JobStatus.QUEUED in JobStatus.ACTIVE
    assert JobStatus.PROCESSING in JobStatus.ACTIVE


def test_terminal_and_active_do_not_overlap():
    """A status is either something a worker runs under, or an outcome."""
    assert not set(JobStatus.ACTIVE) & set(JobStatus.TERMINAL)


def test_every_status_is_either_active_or_terminal():
    """Catches a status added to ALL and to neither group — which would be
    invisible to both the cancel endpoint and the stale-job sweep."""
    assert set(JobStatus.ALL) == set(JobStatus.ACTIVE) | set(JobStatus.TERMINAL)


# ---------------------------------------------------------------------------
# What a worker sees
# ---------------------------------------------------------------------------


def test_worker_stops_when_cancellation_was_requested():
    assert progress_seeing_status(JobStatus.CANCELLING).cancel_requested is True


def test_worker_keeps_going_while_merely_processing():
    assert progress_seeing_status(JobStatus.PROCESSING).cancel_requested is False


def test_worker_keeps_going_before_its_first_advance():
    """
    `_status` is None until the first bookkeeping write. That must read as
    "carry on", not as a cancel — a None that tested truthy would abort every
    job on its first item.
    """
    assert progress_seeing_status(None).cancel_requested is False


def test_a_finished_job_is_not_mistaken_for_a_cancel_request():
    """CANCELLED is the outcome; only CANCELLING asks the worker to stop."""
    assert progress_seeing_status(JobStatus.CANCELLED).cancel_requested is False


# ---------------------------------------------------------------------------
# What the poller sees
# ---------------------------------------------------------------------------


def test_cancelled_job_reports_how_far_it_actually_got():
    """Failure mode 3: a partial run must not claim to be complete."""
    job = make_job(
        status=JobStatus.CANCELLED, total_count=100, processed_count=40
    )
    assert read_job(job).progress_percentage == 40.0


def test_completed_job_still_reads_100_percent():
    """The existing behaviour, which the cancelled branch must not disturb:
    a worker whose source queue drained early is genuinely done."""
    job = make_job(
        status=JobStatus.COMPLETED, total_count=100, processed_count=60
    )
    assert read_job(job).progress_percentage == 100.0


def test_failed_job_still_reads_100_percent():
    job = make_job(status=JobStatus.FAILED, total_count=100, processed_count=3)
    assert read_job(job).progress_percentage == 100.0


def test_cancelled_job_with_no_total_does_not_divide_by_zero():
    job = make_job(status=JobStatus.CANCELLED, total_count=0, processed_count=0)
    assert read_job(job).progress_percentage == 0.0


def test_cancelling_job_reports_live_progress():
    """It is still running, so it reports progress like any running job."""
    job = make_job(
        status=JobStatus.CANCELLING, total_count=50, processed_count=10
    )
    assert read_job(job).progress_percentage == 20.0
