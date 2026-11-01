"""
PassMark's CPU and GPU lists, refreshed into `cpu_benchmarks` / `gpu_benchmarks`.

Plain HTTP with Scrapling's `Fetcher`: the whole table is in the raw HTML, so no
browser. Both pages have the same table, with the same id.

A run below the row floor raises and writes nothing: a challenge page is a 200
too, and would otherwise parse as a successful refresh of 0 rows. Runs are
background jobs (`refresh_worker`), so that failure reaches the Jobs page with
its reason.

The whole run is one transaction, so a run that stops for any reason leaves the
old marks as they were.
"""
import re
from dataclasses import dataclass
from typing import Callable, Optional, Type

from lxml import html as lxml_html
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, SQLModel

from app.benchmark.model import BenchmarkRefresh, CPUBenchmark, GPUBenchmark
from app.common.job_service import JobProgress
from app.scraper.fetch import fetch_html

_LAST_UPDATED = re.compile(r"Last updated on the [^<]{1,60}")
# Progress and the cancel check, in rows. A job update per row would double the run.
_REPORT_EVERY = 500


@dataclass(frozen=True)
class _Source:
    url: str
    model: Type[SQLModel]
    name_column: str
    mark_column: str
    min_rows: int


SOURCES = {
    "cpu": _Source("https://www.cpubenchmark.net/cpu-list/all", CPUBenchmark, "cpu_name", "cpu_mark", 5000),
    "gpu": _Source("https://www.videocardbenchmark.net/gpu_list.php", GPUBenchmark, "gpu_name", "gpu_mark", 2500),
}


def parse_passmark_table(html: str, table_id: str = "cputable") -> list[tuple[str, int]]:
    """(name, mark) per row. A row whose mark is not a number is skipped."""
    rows: list[tuple[str, int]] = []
    for row in lxml_html.fromstring(html).xpath(f"//table[@id='{table_id}']//tbody/tr"):
        cells = row.xpath("./td")
        if len(cells) < 2:
            continue
        mark = cells[1].text_content().replace(",", "").strip()
        if mark.isdigit():
            rows.append((cells[0].text_content().strip(), int(mark)))
    return rows


def _source_updated_text(html: str) -> Optional[str]:
    match = _LAST_UPDATED.search(html)
    return match.group(0).strip() if match else None


def refresh(source_name: str, session: Session, progress: JobProgress) -> dict:
    """Fetch one list and upsert it; the refresh row goes in the same transaction."""
    source = SOURCES[source_name]
    html = fetch_html(source.url)
    rows = parse_passmark_table(html)
    if len(rows) < source.min_rows:
        raise ValueError(
            f"PassMark {source_name} list has {len(rows)} rows, below the floor of "
            f"{source.min_rows}: a challenge page or a changed table, so nothing was written"
        )

    progress.set_total(len(rows))
    for start in range(0, len(rows), _REPORT_EVERY):
        batch = rows[start:start + _REPORT_EVERY]
        for name, mark in batch:
            session.exec(
                insert(source.model)
                .values({source.name_column: name, source.mark_column: mark})
                .on_conflict_do_update(index_elements=[source.name_column], set_={source.mark_column: mark})
            )
        progress.advance(count=len(batch))
        if progress.cancel_requested:
            session.rollback()
            return {"source": source_name, "written": False, "rows_written": 0}

    updated_text = _source_updated_text(html)
    session.add(BenchmarkRefresh(source=source_name, row_count=len(rows), source_updated_text=updated_text))
    session.commit()
    return {
        "source": source_name,
        "written": True,
        "rows_written": len(rows),
        "source_updated_text": updated_text,
    }


def refresh_worker(source_name: str) -> Callable[[Session, JobProgress], dict]:
    """A `worker(session, progress)` for `run_job`."""

    def worker(session: Session, progress: JobProgress) -> dict:
        return refresh(source_name, session, progress)

    return worker
