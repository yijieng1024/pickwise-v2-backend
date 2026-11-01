"""
Which database a script is about to write to.

The test and production databases are both Supabase, so their URLs have the same
shape and differ only by project ref. Every cheaper check ("contains supabase",
"is not localhost") passes on production, so only the ref counts, and a run with no
production ref to compare against refuses rather than guesses.
"""
import os
import re
from typing import Mapping, Optional
from urllib.parse import urlparse

# Supabase project ref, from either connection style:
#   direct  postgresql://postgres:pw@db.<ref>.supabase.co:5432/postgres
#   pooler  postgresql://postgres.<ref>:pw@aws-0-<region>.pooler.supabase.com:5432/postgres
# The pooler puts the ref in the USERNAME, not the host, so one pattern is not
# enough and matching on the host alone silently fails on the pooler URL.
_DIRECT_REF = re.compile(r"(?:^|@|\.)db\.([a-z0-9]{16,})\.supabase\.co", re.I)
_POOLER_REF = re.compile(r"//postgres\.([a-z0-9]{16,})[:@]", re.I)


def project_ref(url: str) -> Optional[str]:
    """The Supabase project ref in `url`, or None if it is not a Supabase URL."""
    if not url:
        return None
    for pattern in (_POOLER_REF, _DIRECT_REF):
        match = pattern.search(url)
        if match:
            return match.group(1).lower()
    return None


def host_of(url: str) -> str:
    """Safe to log: never the username, which carries the ref, or the password."""
    return urlparse(url).hostname or "unknown"


def write_target(variable: str, env: Optional[Mapping[str, str]] = None) -> str:
    """
    The URL a write script may use, the same way alembic/env.py chooses one.
    Production only when *variable* is set to `production` on purpose, and then only
    if the URL really is production; otherwise TEST_DATABASE_URL, which must not be.
    """
    env = os.environ if env is None else env
    production = (env.get("PRODUCTION_DB_REF") or "").strip().lower()
    if not production:
        raise RuntimeError("PRODUCTION_DB_REF is not set, so there is nothing to check this database against.")

    if (env.get(variable) or "").lower() == "production":
        url = env.get("DATABASE_URL")
        if not url:
            raise RuntimeError(f"{variable}=production but DATABASE_URL is unset.")
        if project_ref(url) != production:
            raise RuntimeError(f"{variable}=production but DATABASE_URL is not the production project.")
        return url

    url = env.get("TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(f"No TEST_DATABASE_URL. To write to production on purpose, set {variable}=production.")
    if project_ref(url) == production:
        raise RuntimeError("TEST_DATABASE_URL points at the production project.")
    return url
