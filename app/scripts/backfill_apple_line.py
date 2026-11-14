"""
Give every MacBook Pro row the line token in `model_code` and the name built by
app/processor/apple_name.py. One-off: those rows predate the token, so the next Apple
run would not match them and would insert a second copy of each.

    python -m app.scripts.backfill_apple_line            # prints, writes nothing
    python -m app.scripts.backfill_apple_line --apply

Reads and writes TEST_DATABASE_URL unless APPLE_BACKFILL_TARGET=production is set
(app/common/db_target.py). Safe to run again: a row already in its new form is skipped.
Air and Neo are not touched (one line each, no token).

Which line a row is: there is no link from a laptop to its raw page, so
- a row created before the split pages were first scraped came from the one page that
  existed, /macbook-pro/specs/, the Liquid Retina XDR machine. That includes c07480ce,
  which today's PROLED page overwrote: Jack decided it is restored to the June machine.
- a row created after is linked to the raw page that created it through the processor
  job's result: records run one at a time, so a job's new rows, in created_at order,
  split by each record's `variants_saved`. The raw page's source_url then gives the line
  (apple_name.py). A row with no link, a job whose counts do not add up, or an unknown
  URL is skipped, never guessed: `display_type` is the LLM's and is not used.

Only model_code and product_name. A Core UPDATE, so the mapper event doesn't stamp
`updated_at` and the Dashboard does not call every Pick Score stale.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Connection

from app.common.db_target import host_of, write_target
from app.common.job_model import BackgroundJob
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop
from app.processor.apple_name import UnknownLine, build_name, product_and_line, with_line_token
from app.scraper.models import RawScrapLaptop

_LAPTOPS = Laptop.__table__
_BRANDS = LaptopBrand.__table__
_JOBS = BackgroundJob.__table__
_RAW = RawScrapLaptop.__table__
# The first PROLED / Liquid Retina page (raw 282c613b) was scraped 2026-11-03 06:52:08 UTC.
SPLIT_AT = datetime(2026, 11, 3, 6, 52)
_OLD_PAGE = "https://www.apple.com/my/macbook-pro/specs/"


def read_laptops(connection: Connection) -> list[Any]:
    """Every Apple laptop: a code on any of them is taken."""
    return list(connection.execute(
        select(*(_LAPTOPS.c[n] for n in (
            "id", "model_code", "product_name", "status", "created_at", "display_type",
            "display_size_inch", "processor_model", "cpu_cores", "gpu_cores", "ram_gb", "ssd_gb",
        )))
        .join(_BRANDS, _BRANDS.c.id == _LAPTOPS.c.brand_id)
        .where(func.lower(_BRANDS.c.name) == "apple")
        .order_by(_LAPTOPS.c.created_at, _LAPTOPS.c.model_code)
    ))


def read_jobs(connection: Connection) -> list[Any]:
    """Processor runs since the split, oldest first."""
    return list(connection.execute(
        select(_JOBS.c.id, _JOBS.c.started_at, _JOBS.c.finished_at, _JOBS.c.result)
        .where(_JOBS.c.job_type == "processor.process_pending", _JOBS.c.started_at >= SPLIT_AT)
        .order_by(_JOBS.c.started_at)
    ))


def read_raw_urls(connection: Connection) -> dict[str, str]:
    return {str(r.id): r.source_url for r in connection.execute(select(_RAW.c.id, _RAW.c.source_url))}


def _utc(moment: datetime) -> datetime:
    """Naive UTC: jobs store timestamptz, laptops.created_at naive UTC."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def link_new_rows(jobs: Iterable[Any], laptops: Iterable[Any], raw_urls: Mapping[str, str]) -> dict[Any, str]:
    """laptop id -> source_url of the raw page that created it. A job whose new rows do
    not add up to its records' variants_saved links nothing."""
    laptops = list(laptops)
    links = {}
    for job in jobs:
        start, end = _utc(job.started_at), _utc(job.finished_at or datetime.max)
        # Apple rows only: a job that also made another brand's rows does not add up, and
        # links nothing.
        made = sorted((l for l in laptops if start <= _utc(l.created_at) <= end), key=lambda l: l.created_at)
        records = [d for d in (job.result or {}).get("details", []) if d.get("variants_saved")]
        if len(made) != sum(d["variants_saved"] for d in records):
            continue
        for record in records:
            for laptop in made[:record["variants_saved"]]:
                if record["raw_id"] in raw_urls:
                    links[laptop.id] = raw_urls[record["raw_id"]]
            made = made[record["variants_saved"]:]
    return links


def line_of(laptop: Any, links: Mapping[Any, str]) -> tuple[Optional[str], str]:
    """(line, where it came from); line None means skip, with the reason."""
    if _utc(laptop.created_at) < SPLIT_AT:
        return product_and_line(_OLD_PAGE)[1], "old page"
    url = links.get(laptop.id)
    if url is None:
        return None, "no raw page linked"
    try:
        return product_and_line(url)[1], url
    except UnknownLine:
        return None, f"unknown page: {url}"


def plan(laptops: Iterable[Any], links: Mapping[Any, str] = {}) -> tuple[list[tuple[Any, str, str]], list[tuple[Any, str]]]:
    """(laptop, new code, new name) and (laptop, why not). Writes nothing."""
    laptops = list(laptops)
    pro = [l for l in laptops if l.model_code.startswith("apple-macbook-pro-")]
    writes, skips = [], []
    for laptop in pro:
        line, source = line_of(laptop, links)
        if line is None:
            skips.append((laptop, source))
            continue
        code = with_line_token(laptop.model_code, line)
        name = build_name("MacBook Pro", line, laptop.display_size_inch, laptop.processor_model,
                          laptop.cpu_cores, laptop.gpu_cores, laptop.ram_gb, laptop.ssd_gb)
        if code == laptop.model_code and name == laptop.product_name:
            skips.append((laptop, "already done"))
        elif code == laptop.model_code and line not in code:
            skips.append((laptop, f"code has no place for the token: {code}"))
        else:
            writes.append((laptop, code, name))

    # The unique index: a new code must not be one another row holds or will hold.
    kept = {l.model_code for l in laptops} - {l.model_code for l, c, _ in writes if c != l.model_code}
    new_codes = [c for _, c, _ in writes]
    clashes = {c for c in new_codes if c in kept or new_codes.count(c) > 1}
    skips += [(l, f"collides with an existing code: {c}") for l, c, _ in writes if c in clashes]
    return [w for w in writes if w[1] not in clashes], skips


def apply(connection: Connection, writes: list[tuple[Any, str, str]]) -> int:
    """Rows changed. The caller owns the transaction, so a failure writes nothing."""
    return sum(
        connection.execute(
            update(_LAPTOPS)
            .where(_LAPTOPS.c.id == laptop.id, _LAPTOPS.c.model_code == laptop.model_code)
            .values(model_code=code, product_name=name)
        ).rowcount
        for laptop, code, name in writes
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("APPLE_BACKFILL_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")

    engine = create_engine(url)
    with engine.begin() as connection:
        if not args.apply:
            connection.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")  # this one, already open
        laptops = read_laptops(connection)
        links = link_new_rows(read_jobs(connection), laptops, read_raw_urls(connection))
        writes, skips = plan(laptops, links)
        for laptop, code, name in writes:
            print(f"WRITE {laptop.id} [{laptop.status}] from {line_of(laptop, links)[1]}\n"
                  f"  {laptop.model_code} -> {code}\n"
                  f"  {laptop.product_name} -> {name}")
        for laptop, reason in skips:
            print(f"SKIP  {laptop.id} [{laptop.status}] {reason}")
        changed = apply(connection, writes) if args.apply else 0
    engine.dispose()

    print(f"{len(writes)} to write, {len(skips)} skipped, {changed} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
