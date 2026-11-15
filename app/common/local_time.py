"""The business timezone, and the bounds of "today" in it.

PickWise serves Malaysia and its admins read "today" as the Malaysian day, so
a count that resets at UTC midnight (08:00 here) still shows yesterday for the
first eight hours of every morning.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Kuala_Lumpur")


def local_day_utc_naive(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """(start, end) of the current Malaysian day as naive UTC datetimes, for
    filtering a naive-UTC column with `start <= created_at < end`."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(LOCAL_TZ).date()
    start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )
