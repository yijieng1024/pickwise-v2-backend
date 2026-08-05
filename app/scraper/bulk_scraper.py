"""
bulk_scraper.py
---------------
Orchestrates scrape runs over ScrapeTarget rows, in two modes:

  - run_bulk_scrape(brand_id)      — every pending/failed target for one brand
  - run_scrape_for_targets(ids)    — an explicit set of targets the admin picked
                                     out of the crawled queue

Both share the same per-target pipeline (_process_target).

Responsibilities:
  - Query URLs from laptop_scrape_urls
  - Dispatch to the correct brand scraper (Apple / ASUS / Acer / …)
  - Stamp last_scraped_at regardless of outcome
  - Persist successful results to raw_scrap_laptops
  - Write a timestamped failure log file if any URL errors out
  - Return a structured BulkScrapeReport for the API layer
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import or_
from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.scraper.apple_scraper import scrape_official_website
from app.scraper.asus_scraper import scrape_asus_laptop_specs
from app.scraper.acer_scraper import scrape_acer_laptop_specs
from app.scraper.models import RawScrapLaptop, ScrapeStatus, ScrapeTarget

# Brands whose pages are uploaded by an admin and read from `raw_product_htmls`
# rather than fetched live. See app/scraper/acer_scraper.py for why.
HTML_UPLOAD_BRANDS = {"acer"}

# ---------------------------------------------------------------------------
# Log directory (relative to the project root, created on first use)
# ---------------------------------------------------------------------------

_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),  # project root
    "logs",
    "scraper",
)


# ---------------------------------------------------------------------------
# Result dataclass (serialised by the router into the HTTP response)
# ---------------------------------------------------------------------------

@dataclass
class UrlResult:
    url: str
    status: str          # "succeeded" | "skipped" | "failed"
    error: Optional[str] = None


@dataclass
class BulkScrapeReport:
    brand_name: str
    total_pending: int
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    log_file: Optional[str] = None
    results: List[UrlResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _write_failure_log(brand_name: str, run_ts: datetime, report: BulkScrapeReport) -> str:
    """
    Write a plain-text failure log and return the absolute path of the file.
    The log directory is created automatically if it does not exist.
    """
    os.makedirs(_LOG_DIR, exist_ok=True)

    # Sanitise brand name for use in a filename (replace spaces with underscores)
    safe_brand = brand_name.replace(" ", "_").lower()
    ts_str = run_ts.strftime("%Y%m%d_%H%M%S")
    log_filename = f"bulk_scrape_{safe_brand}_{ts_str}.log"
    log_path = os.path.join(_LOG_DIR, log_filename)

    failed_results = [r for r in report.results if r.status == "failed"]

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"Bulk Scrape Run — {brand_name} — {run_ts.isoformat()}\n")
        f.write(f"Total Pending : {report.total_pending}\n")
        f.write(f"Failed        : {report.failed}\n")
        f.write("-" * 60 + "\n\n")

        for entry in failed_results:
            f.write(f"[FAILED] {entry.url}\n")
            f.write(f"Error  : {entry.error}\n\n")

    return log_path


async def _dispatch_scraper(
    brand_name: str, url: str, brand_id: UUID, session: Session
) -> list[dict]:
    """
    Route a URL to the correct brand scraper.
    Always returns a list — one dict per variant found.
    Raises ValueError for unsupported brands.

    *session* is only used by HTML-upload brands (Acer), which read their page
    out of `raw_product_htmls` instead of fetching it.
    """
    name = brand_name.lower()

    if name == "apple":
        # Apple scraper returns a single dict; wrap in list for uniformity
        result = await scrape_official_website(url, brand_name, str(brand_id))
        return [result]
    elif name == "asus":
        # ASUS scraper already returns list[dict] (one per variant)
        return await scrape_asus_laptop_specs(url, brand_id)
    elif name == "acer":
        # Acer scraper returns list[dict] — always one (simple products)
        return await scrape_acer_laptop_specs(url, brand_id, session)
    else:
        raise ValueError(f"Bulk scraping is not supported for brand: {brand_name}")


def _success_status(brand_name: str) -> str:
    """
    Terminal status for a successful run.

    HTML-upload brands land on `parsed` rather than `completed`, so the admin UI
    can tell a hand-uploaded page apart from a live scrape.
    """
    return (
        ScrapeStatus.PARSED
        if brand_name.lower() in HTML_UPLOAD_BRANDS
        else ScrapeStatus.COMPLETED
    )


def _update_target(session: Session, target: ScrapeTarget, scrape_status: str) -> None:
    """Set last_scraped_at and scrape_status, then persist."""
    target.last_scraped_at = datetime.now(timezone.utc)
    target.scrape_status = scrape_status
    session.merge(target)
    session.commit()


async def _process_target(
    session: Session,
    target: ScrapeTarget,
    brand: LaptopBrand,
    report: BulkScrapeReport,
    progress=None,
) -> None:
    """
    Scrape a single target and record the outcome in *report*.

    Shared by the whole-brand and admin-selected runs so both stamp statuses
    and persist raw rows identically.
    """
    url = target.url
    report.processed += 1

    # Skip if ANY variant of this URL was already stored
    # (source_url is either bare URL or URL?v=N for multi-variant pages)
    already_scraped = session.exec(
        select(RawScrapLaptop).where(
            RawScrapLaptop.source_url.like(f"{url}%")  # type: ignore[arg-type]
        )
    ).first()

    if already_scraped:
        report.skipped += 1
        report.results.append(UrlResult(url=url, status="skipped"))
        _update_target(session, target, ScrapeStatus.SKIPPED)
        # Skipped still counts as processed — the progress bar tracks targets
        # examined, not just those that produced new data.
        if progress:
            progress.advance(succeeded=True, item=url)
        return

    # Dispatch to the brand scraper (returns list — one item per variant)
    try:
        variant_results = await _dispatch_scraper(brand.name, url, brand.id, session)
    except Exception as exc:
        _update_target(session, target, ScrapeStatus.FAILED)
        report.failed += 1
        report.results.append(UrlResult(url=url, status="failed", error=str(exc)))
        if progress:
            progress.advance(succeeded=False, item=url, error=str(exc))
        return

    # Process each variant result
    url_had_failure = False
    url_variants_saved = 0

    for variant in variant_results:
        if variant.get("status") == "failed":
            error_msg = variant.get("error", "Unknown scraper error")
            report.failed += 1
            report.results.append(UrlResult(url=url, status="failed", error=error_msg))
            url_had_failure = True
            continue

        # Build unique source_url: bare URL for single variants,
        # URL?v=N for pages with multiple variants.
        suffix = variant.get("source_url_suffix", "")
        source_url = f"{url}{suffix}"

        raw_laptop = RawScrapLaptop(
            source_url=source_url,
            brand_id=brand.id,
            raw_product_name=variant.get("product_name", "Unknown Model"),
            raw_prices=variant.get("raw_prices_list", []),
            image_urls=variant.get("image_urls", []),
            raw_specs_dump={"scraped_features": variant.get("raw_specs", [])},
            processing_status="pending",
        )
        session.add(raw_laptop)
        session.commit()
        url_variants_saved += 1

    # Stamp status based on outcome
    final_status = (
        _success_status(brand.name) if url_variants_saved > 0 else ScrapeStatus.FAILED
    )
    _update_target(session, target, final_status)

    if not url_had_failure:
        report.succeeded += 1
        report.results.append(
            UrlResult(
                url=url,
                status="succeeded",
                error=f"{url_variants_saved} variant(s) saved" if url_variants_saved > 1 else None,
            )
        )

    if progress:
        progress.advance(
            succeeded=not url_had_failure,
            item=url,
            error="One or more variants failed — see the run report" if url_had_failure else None,
        )


def _finalise(report: BulkScrapeReport, run_ts: datetime) -> BulkScrapeReport:
    """Write the failure log (if needed) and emit the console summary."""
    if report.failed > 0:
        log_path = _write_failure_log(report.brand_name, run_ts, report)
        report.log_file = log_path
        print(f"⚠️  Scrape completed with {report.failed} failure(s). Log: {log_path}")
    else:
        print(f"✅ Scrape completed successfully. {report.succeeded} URL(s) scraped.")

    return report


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_bulk_scrape(
    brand_id: UUID, session: Session, progress=None
) -> BulkScrapeReport:
    """
    Perform a full bulk scrape for all pending (last_scraped_at IS NULL)
    ScrapeTarget rows that belong to *brand_id*.

    Returns a BulkScrapeReport describing the outcome of every URL processed.
    """
    run_ts = datetime.now(timezone.utc)

    # 1. Validate brand
    brand: Optional[LaptopBrand] = session.get(LaptopBrand, brand_id)
    if brand is None:
        raise ValueError(f"Brand with id={brand_id} not found.")

    # 2. Query pending + previously-failed + newly-uploaded URLs for this brand.
    #    `html_uploaded` must be here explicitly: a target that failed for want
    #    of HTML already has last_scraped_at stamped, so fresh HTML arriving
    #    afterwards would otherwise never be re-picked.
    pending_targets: List[ScrapeTarget] = list(
        session.exec(
            select(ScrapeTarget).where(
                ScrapeTarget.brand_id == brand_id,
                ScrapeTarget.is_active == True,        # noqa: E712
                or_(
                    ScrapeTarget.last_scraped_at == None,  # noqa: E711
                    ScrapeTarget.scrape_status == ScrapeStatus.FAILED,
                    ScrapeTarget.scrape_status == ScrapeStatus.HTML_UPLOADED,
                ),
            )
        ).all()
    )

    report = BulkScrapeReport(
        brand_name=brand.name,
        total_pending=len(pending_targets),
    )

    if progress:
        progress.set_total(len(pending_targets))

    if not pending_targets:
        return report

    # 3. Iterate and scrape
    for target in pending_targets:
        await _process_target(session, target, brand, report, progress)

    # 4. Write failure log if any URLs failed
    return _finalise(report, run_ts)


async def run_scrape_for_targets(
    target_ids: List[UUID], session: Session, progress=None
) -> BulkScrapeReport:
    """
    Scrape an explicit set of ScrapeTarget rows — the laptops an admin ticked
    in the crawled queue.

    Unlike run_bulk_scrape this ignores scrape_status entirely: if the admin
    selected a row, they want it scraped. Already-stored URLs still report as
    "skipped" so nothing is duplicated.

    Raises ValueError if any requested id does not exist.
    """
    run_ts = datetime.now(timezone.utc)

    if not target_ids:
        raise ValueError("No target_ids provided.")

    targets: List[ScrapeTarget] = list(
        session.exec(
            select(ScrapeTarget).where(ScrapeTarget.id.in_(target_ids))  # type: ignore[attr-defined]
        ).all()
    )

    found_ids = {t.id for t in targets}
    missing = [str(tid) for tid in target_ids if tid not in found_ids]
    if missing:
        raise ValueError(f"ScrapeTarget id(s) not found: {', '.join(missing)}")

    # Resolve each target's brand once — a selection may span brands
    brand_cache: dict[UUID, LaptopBrand] = {}
    for target in targets:
        if target.brand_id not in brand_cache:
            brand = session.get(LaptopBrand, target.brand_id)
            if brand is None:
                raise ValueError(
                    f"Brand with id={target.brand_id} (referenced by target "
                    f"{target.id}) not found."
                )
            brand_cache[target.brand_id] = brand

    brand_names = sorted({b.name for b in brand_cache.values()})
    report = BulkScrapeReport(
        brand_name=brand_names[0] if len(brand_names) == 1 else "+".join(brand_names),
        total_pending=len(targets),
    )

    if progress:
        progress.set_total(len(targets))

    # Preserve the caller's ordering rather than the DB's
    order = {tid: i for i, tid in enumerate(target_ids)}
    for target in sorted(targets, key=lambda t: order[t.id]):
        await _process_target(
            session, target, brand_cache[target.brand_id], report, progress
        )

    return _finalise(report, run_ts)
