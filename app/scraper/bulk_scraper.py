"""
bulk_scraper.py
---------------
Orchestrates a bulk scrape run for all ScrapeTarget rows belonging to a
given brand where last_scraped_at IS NULL.

Responsibilities:
  - Query pending URLs from laptop_scrape_urls
  - Dispatch to the correct brand scraper (Apple / ASUS / …)
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

from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.scraper.apple_scraper import scrape_official_website
from app.scraper.asus_scraper import scrape_asus_laptop_specs
from app.scraper.models import RawScrapLaptop, ScrapeTarget

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


async def _dispatch_scraper(brand_name: str, url: str, brand_id: UUID) -> list[dict]:
    """
    Route a URL to the correct brand scraper.
    Always returns a list — one dict per variant found.
    Raises ValueError for unsupported brands.
    """
    name = brand_name.lower()

    if name == "apple":
        # Apple scraper returns a single dict; wrap in list for uniformity
        result = await scrape_official_website(url, brand_name, str(brand_id))
        return [result]
    elif name == "asus":
        # ASUS scraper already returns list[dict] (one per variant)
        return await scrape_asus_laptop_specs(url, brand_id)
    else:
        raise ValueError(f"Bulk scraping is not supported for brand: {brand_name}")


def _stamp_last_scraped(session: Session, target: ScrapeTarget) -> None:
    """Set last_scraped_at to now and persist the change."""
    target.last_scraped_at = datetime.now(timezone.utc)
    session.merge(target)
    session.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_bulk_scrape(brand_id: UUID, session: Session) -> BulkScrapeReport:
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

    # 2. Query pending URLs for this brand
    pending_targets: List[ScrapeTarget] = list(
        session.exec(
            select(ScrapeTarget).where(
                ScrapeTarget.brand_id == brand_id,
                ScrapeTarget.last_scraped_at == None,  # noqa: E711
                ScrapeTarget.is_active == True,        # noqa: E712
            )
        ).all()
    )

    report = BulkScrapeReport(
        brand_name=brand.name,
        total_pending=len(pending_targets),
    )

    if not pending_targets:
        return report

    # 3. Iterate and scrape
    for target in pending_targets:
        url = target.url
        report.processed += 1

        # 3a. Skip if ANY variant of this URL was already stored
        # (source_url is either bare URL or URL?v=N for multi-variant pages)
        already_scraped = session.exec(
            select(RawScrapLaptop).where(
                RawScrapLaptop.source_url.like(f"{url}%")  # type: ignore[arg-type]
            )
        ).first()

        if already_scraped:
            report.skipped += 1
            report.results.append(UrlResult(url=url, status="skipped"))
            _stamp_last_scraped(session, target)
            continue

        # 3b. Dispatch to the brand scraper (returns list — one item per variant)
        try:
            variant_results = await _dispatch_scraper(brand.name, url, brand_id)
        except Exception as exc:
            _stamp_last_scraped(session, target)
            report.failed += 1
            report.results.append(UrlResult(url=url, status="failed", error=str(exc)))
            continue

        # 3c. Always stamp last_scraped_at regardless of outcome
        _stamp_last_scraped(session, target)

        # 3d. Process each variant result
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
                brand_id=brand_id,
                raw_product_name=variant.get("product_name", "Unknown Model"),
                raw_prices=variant.get("raw_prices_list", []),
                image_urls=variant.get("image_urls", []),
                raw_specs_dump={"scraped_features": variant.get("raw_specs", [])},
                processing_status="pending",
            )
            session.add(raw_laptop)
            session.commit()
            url_variants_saved += 1

        if not url_had_failure:
            report.succeeded += 1
            report.results.append(
                UrlResult(
                    url=url,
                    status="succeeded",
                    error=f"{url_variants_saved} variant(s) saved" if url_variants_saved > 1 else None,
                )
            )

    # 4. Write failure log if any URLs failed
    if report.failed > 0:
        log_path = _write_failure_log(brand.name, run_ts, report)
        report.log_file = log_path
        print(f"⚠️  Bulk scrape completed with {report.failed} failure(s). Log: {log_path}")
    else:
        print(f"✅ Bulk scrape completed successfully. {report.succeeded} URLs scraped.")

    return report
