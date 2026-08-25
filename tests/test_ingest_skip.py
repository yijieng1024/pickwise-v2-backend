"""What ingest does with a video it has already seen.

The dismissal feature rests entirely on one clause: ingest_for_laptop must
leave an `irrelevant` row alone. If it ever rewrote one, every dismissal would
be undone by the next ingest run and the queue would refill with the same
non-laptop videos — the failure the feature exists to prevent, and one that
would show up as "the dismiss button doesn't work" long after the change that
caused it.

These assert against service.should_skip_existing, the function ingest_for_laptop
actually calls, not a copy of its logic. No database and no network: the
predicate is pure, which is why it was extracted.

Run: pytest tests/ -q
"""

import pytest

from app.reviews.models import RawYoutubeReview, ReviewStatus
from app.reviews.service import should_skip_existing
from app.reviews.transcript import TranscriptFailure


def _row(status: str, failure_reason: str | None = None) -> RawYoutubeReview:
    return RawYoutubeReview(
        video_id="vid",
        channel_id="UC0",
        video_title="t",
        status=status,
        failure_reason=failure_reason,
    )


def test_irrelevant_is_skipped():
    """The whole point. A dismissed video stays dismissed across ingest runs."""
    assert should_skip_existing(_row(ReviewStatus.IRRELEVANT.value), True) is True


def test_irrelevant_is_skipped_in_discovery_only_mode():
    assert should_skip_existing(_row(ReviewStatus.IRRELEVANT.value), False) is True


def test_matched_and_pending_are_skipped():
    for status in (ReviewStatus.MATCHED.value, ReviewStatus.PENDING.value):
        assert should_skip_existing(_row(status), True) is True


def test_rejected_is_retried_when_failure_was_operational():
    """Unchanged behaviour, asserted so the irrelevant clause cannot break it:
    an IP block or a timeout is not evidence that the captions are missing."""
    row = _row(ReviewStatus.REJECTED.value, TranscriptFailure.IP_BLOCKED.value)
    assert should_skip_existing(row, True) is False


def test_rejected_with_terminal_failure_is_skipped():
    row = _row(ReviewStatus.REJECTED.value, TranscriptFailure.NO_TRACK.value)
    assert should_skip_existing(row, True) is True


def test_rejected_is_skipped_in_discovery_only_mode():
    row = _row(ReviewStatus.REJECTED.value, TranscriptFailure.IP_BLOCKED.value)
    assert should_skip_existing(row, False) is True


def test_unseen_video_is_not_skipped():
    assert should_skip_existing(None, True) is False


@pytest.mark.parametrize("status", [s.value for s in ReviewStatus])
def test_only_rejected_can_ever_be_rewritten(status):
    """A structural guard on the clause itself, so a future status member is a
    decision rather than an accident: any status other than `rejected` must
    skip. A new state that should be retryable has to change this test, which
    is the moment to think about it."""
    skipped = should_skip_existing(_row(status), True)
    assert skipped is (status != ReviewStatus.REJECTED.value)
