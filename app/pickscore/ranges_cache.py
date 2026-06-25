import time
from typing import Optional

_caches: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # 5 minutes


def get_cached_ranges(cache_key: str) -> Optional[dict]:
    if cache_key in _caches:
        data, ts = _caches[cache_key]
        if (time.time() - ts) < CACHE_TTL:
            return data
    return None


def set_cached_ranges(cache_key: str, data: dict) -> None:
    _caches[cache_key] = (data, time.time())
