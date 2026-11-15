"""The Malaysian day used for "today" counts (agent monitoring /stats)."""

from datetime import datetime, timezone

from ._adapters import local_day_utc_naive


def test_before_8am_malaysia_is_already_the_new_day():
    # 23:30 UTC on the 14th is 07:30 on the 15th in Malaysia.
    start, end = local_day_utc_naive(datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc))
    assert start == datetime(2026, 11, 14, 16, 0)  # 00:00 MYT on the 15th
    assert end == datetime(2026, 11, 15, 16, 0)
    assert start.tzinfo is None and end.tzinfo is None


def test_late_utc_evening_rolls_into_the_next_malaysian_day():
    # 16:00 UTC is exactly midnight MYT: the new day starts here, inclusive.
    start, _ = local_day_utc_naive(datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc))
    assert start == datetime(2026, 11, 15, 16, 0)
    start, _ = local_day_utc_naive(datetime(2026, 11, 15, 15, 59, tzinfo=timezone.utc))
    assert start == datetime(2026, 11, 14, 16, 0)
