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

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import or_
from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.processor.apple_name import SPECS_PAGES
from app.processor.engine import raw_sku
from app.scraper.apple_options_fetch import run_options_step
from app.scraper.apple_scraper import crawl_apple_specs_links, scrape_apple_buy_page, scrape_official_website
from app.scraper.asus_scraper import crawl_asus_specs_links, scrape_asus_laptop_specs
from app.scraper.acer_scraper import crawl_acer_specs_links, scrape_acer_laptop_specs
from app.scraper.hp_scraper import crawl_hp_specs_links, scrape_hp_laptop_specs
from app.scraper.models import RawScrapLaptop, ScrapeStatus, ScrapeTarget

# Brands whose pages are uploaded by an admin and read from `raw_product_htmls`
# rather than fetched live. See app/scraper/acer_scraper.py for why.
HTML_UPLOAD_BRANDS = {"acer"}

# Pause after every live fetch in a bulk run. A browser page load was slow
# enough to need none; plain HTTP would hit a site back to back.
LIVE_FETCH_PAUSE_S = 1.5


Crawl = Callable[[str, Session, UUID], Awaitable[list[str]]]
Scrape = Callable[[str, UUID, Session], Awaitable[list[dict]]]


# The brands' own functions differ, so these give every brand one signature.
# Only Acer uses the session: it reads uploaded pages from raw_product_htmls.

async def _crawl_apple(start_url: str, session: Session, brand_id: UUID) -> list[str]:
    return await crawl_apple_specs_links(start_url)


async def _crawl_asus(start_url: str, session: Session, brand_id: UUID) -> list[str]:
    return await crawl_asus_specs_links(start_url)


async def _crawl_acer(start_url: str, session: Session, brand_id: UUID) -> list[str]:
    return await crawl_acer_specs_links(start_url, session, brand_id)


async def _crawl_hp(start_url: str, session: Session, brand_id: UUID) -> list[str]:
    return await crawl_hp_specs_links(start_url)


async def _scrape_apple(url: str, brand_id: UUID, session: Session) -> list[dict]:
    # Apple's scraper returns a single dict, every other brand a list
    if url in SPECS_PAGES:
        return [await scrape_apple_buy_page(url, str(brand_id))]
    return [await scrape_official_website(url, "Apple", str(brand_id))]


async def _scrape_asus(url: str, brand_id: UUID, session: Session) -> list[dict]:
    return await scrape_asus_laptop_specs(url, brand_id)


async def _scrape_acer(url: str, brand_id: UUID, session: Session) -> list[dict]:
    return await scrape_acer_laptop_specs(url, brand_id, session)


async def _scrape_hp(url: str, brand_id: UUID, session: Session) -> list[dict]:
    return await scrape_hp_laptop_specs(url, brand_id)


# The one place that says which brands exist: lowercase brand name -> (crawl,
# scrape). Both routes and the bulk path read it, so a new brand is one line.
SCRAPERS: dict[str, tuple[Crawl, Scrape]] = {
    "apple": (_crawl_apple, _scrape_apple),
    "asus": (_crawl_asus, _scrape_asus),
    "acer": (_crawl_acer, _scrape_acer),
    "hp": (_crawl_hp, _scrape_hp),
}

# The admin Dashboard uses this to say "No scraper" instead of "never scraped".
SCRAPER_BRANDS = frozenset(SCRAPERS)

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
    #: True when the run stopped because the job was cancelled, as opposed to
    #: running out of targets. The counters above are then a partial tally.
    cancelled: bool = False
    log_file: Optional[str] = None
    results: List[UrlResult] = field(default_factory=list)
    #: Step 10c: Apple build-to-order options, fetched after a run that scraped an Apple
    #: buy page. None when no buy page was scraped.
    apple_options: Optional[dict] = None


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
    scrapers = SCRAPERS.get(brand_name.lower())
    if scrapers is None:
        raise ValueError(f"Bulk scraping is not supported for brand: {brand_name}")
    _, scrape = scrapers
    return await scrape(url, brand_id, session)


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


# Sites whose pages carry the maker's SKU (plan step 11, decision 6): the processor
# finds the same laptop by it, so a re-scrape updates a row and makes no second one.
# www.asus.com (TUF included) has no SKU and goes by the LLM's slug: new URLs only.
RESCRAPED_HOSTS = frozenset({"rog.asus.com", "www.hp.com"})


def rescraped_every_run(url: str) -> bool:
    """A page a bulk run fetches again every time, not only when new or failed."""
    return urlsplit(url).netloc.lower() in RESCRAPED_HOSTS


def rescrapable(brand_name: str, url: str) -> bool:
    """A page fetched again although it is stored, the new fetch replacing its raw row.
    Apple specs pages (decided 2026-11-03): the buy page needs today's specs, and a stored
    June page would otherwise be read forever. ROG and HP pages: see RESCRAPED_HOSTS.
    Every other page keeps the skip rule."""
    return (brand_name.lower() == "apple" and url.rstrip("/").endswith("/specs")) or rescraped_every_run(url)


def bulk_targets(session: Session, brand_id: UUID) -> List[ScrapeTarget]:
    """
    What a bulk run of one brand scrapes: targets never scraped, failed, or with newly
    uploaded HTML, and every page that is re-scraped each run. `html_uploaded` is named
    because a target that failed for want of HTML already has last_scraped_at stamped,
    so fresh HTML arriving afterwards would otherwise never be re-picked.
    """
    active = session.exec(
        select(ScrapeTarget)
        .where(ScrapeTarget.brand_id == brand_id, ScrapeTarget.is_active == True)  # noqa: E712
        .order_by(ScrapeTarget.created_at, ScrapeTarget.id)  # type: ignore[arg-type]
    ).all()
    return [
        t for t in active
        if t.last_scraped_at is None
        or t.scrape_status in (ScrapeStatus.FAILED, ScrapeStatus.HTML_UPLOADED)
        or rescraped_every_run(t.url)
    ]


def unchanged(stored: RawScrapLaptop, fresh: RawScrapLaptop) -> bool:
    """
    Whether a re-scrape found what is stored (decision 7): the product name, the prices
    and the specs are equal. Those are everything the processor reads from a raw row, so
    an unchanged row would be extracted into the same laptop: it is not sent again, which
    saves LLM quota and keeps hand fixes on a laptop whose page did not change. Photos
    are left out: a new image URL alone is not a reason to process a laptop again.
    """
    return (
        stored.raw_product_name == fresh.raw_product_name
        and stored.raw_prices == fresh.raw_prices
        and stored.raw_specs_dump == fresh.raw_specs_dump
    )


def stored_rows_of(url: str):
    """The raw rows of one page: the bare URL, or URL?v=N for a multi-variant page. Not a
    prefix match: .../buy-mac/macbook-pro is a prefix of .../buy-mac/macbook-pro-proled-xdr."""
    return or_(RawScrapLaptop.source_url == url, RawScrapLaptop.source_url.like(f"{url}?v=%"))  # type: ignore[attr-defined]


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
    already_scraped = not rescrapable(brand.name, url) and session.exec(
        select(RawScrapLaptop).where(stored_rows_of(url))
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

    # Release the pooled connection before the scrape. The `already_scraped`
    # read above holds a transaction open, and a Session does not return its
    # connection until that transaction ends -- so without this it sits
    # idle-in-transaction for every page fetch below, for every
    # target in the run. Materialise the brand fields first: commit expires
    # loaded ORM objects, so reading `brand.name` in the call itself would
    # lazy-refresh and re-open the transaction this commit closes.
    brand_name, brand_pk = brand.name, brand.id
    session.commit()

    # Dispatch to the brand scraper (returns list — one item per variant)
    try:
        variant_results = await _dispatch_scraper(brand_name, url, brand_pk, session)
    except Exception as exc:
        _update_target(session, target, ScrapeStatus.FAILED)
        report.failed += 1
        report.results.append(UrlResult(url=url, status="failed", error=str(exc)))
        if progress:
            progress.advance(succeeded=False, item=url, error=str(exc))
        return
    finally:
        if brand_name.lower() not in HTML_UPLOAD_BRANDS:
            await asyncio.sleep(LIVE_FETCH_PAUSE_S)

    # Process each variant result
    url_had_failure = False
    url_variants_saved = 0
    url_variants_kept = 0  # re-scraped, stored row left as it is

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
            brand_id=brand_pk,
            raw_product_name=variant.get("product_name", "Unknown Model"),
            raw_prices=variant.get("raw_prices_list", []),
            image_urls=variant.get("image_urls", []),
            raw_specs_dump={
                "scraped_features": variant.get("raw_specs", []),
                # Apple buy pages: the configurations, part numbers and prices
                **({"buy_page": variant["buy_page"]} if "buy_page" in variant else {}),
            },
            processing_status="pending",
        )
        # A re-scraped page replaces its raw row (source_url is unique), as an Apple specs page does.
        stored = rescrapable(brand_name, url) and session.exec(
            select(RawScrapLaptop).where(RawScrapLaptop.source_url == source_url)
        ).first()
        # ROG and HP: the stored row stays when nothing changed, and when the page
        # fetched now has no SKU, because processing it again would then go by the slug.
        if stored and rescraped_every_run(url) and (
            raw_sku(raw_laptop.raw_specs_dump) is None or unchanged(stored, raw_laptop)
        ):
            url_variants_kept += 1
            continue
        if stored:
            session.delete(stored)
            session.flush()
        session.add(raw_laptop)
        session.commit()
        url_variants_saved += 1

    # Stamp status based on outcome
    final_status = (
        _success_status(brand_name) if url_variants_saved + url_variants_kept > 0 else ScrapeStatus.FAILED
    )
    _update_target(session, target, final_status)

    if not url_had_failure and url_variants_saved == 0 and url_variants_kept:
        # Fetched, and nothing to process: the page is as stored.
        report.skipped += 1
        report.results.append(UrlResult(url=url, status="skipped", error="unchanged since the last scrape"))
    elif not url_had_failure:
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


async def _apple_options_after_buy_pages(session: Session, report: BulkScrapeReport) -> None:
    """Step 10c: after a run that scraped an Apple buy page, fetch every standard config's
    options. Its failure is reported, never the scrape's."""
    if report.cancelled or not any(r.url in SPECS_PAGES and r.status == "succeeded" for r in report.results):
        return
    try:
        report.apple_options = await run_options_step(session)
    except Exception as exc:
        session.rollback()
        report.apple_options = {"error": f"{type(exc).__name__}: {exc}"}


def _finalise(report: BulkScrapeReport, run_ts: datetime) -> BulkScrapeReport:
    """Write the failure log (if needed) and emit the console summary."""
    if report.failed > 0:
        log_path = _write_failure_log(report.brand_name, run_ts, report)
        report.log_file = log_path
        print(f"⚠️  Scrape completed with {report.failed} failure(s). Log: {log_path}")
    else:
        print(f"✅ Scrape completed successfully. {report.succeeded} URL(s) scraped.")

    return report


async def run_feed_crawl(brand_id: UUID, session: Session) -> dict:
    """
    Crawl one brand's site from its own `base_scrape_url` and queue every URL
    not yet in laptop_scrape_urls. Returns the counts, in total and per site
    (one ASUS crawl covers www.asus.com and rog.asus.com).
    """
    brand: Optional[LaptopBrand] = session.get(LaptopBrand, brand_id)
    if brand is None:
        raise ValueError(f"Brand with id={brand_id} not found.")
    brand_name, start_url = brand.name, (brand.base_scrape_url or "").strip()
    scrapers = SCRAPERS.get(brand_name.lower())
    if scrapers is None:
        raise ValueError(f"Crawling is not supported for brand: {brand_name}")
    # Release the pooled connection before the crawl: a brand-wide crawl walks
    # every listing page, and the read above left a transaction open.
    session.commit()

    crawl, _ = scrapers
    found_urls = await crawl(start_url, session, brand_id)

    sites: dict[str, dict[str, int]] = {}
    added = 0
    for url in found_urls:
        site = sites.setdefault(urlsplit(url).netloc, {"pages_found": 0, "targets_added": 0})
        site["pages_found"] += 1
        if not session.exec(select(ScrapeTarget).where(ScrapeTarget.url == url)).first():
            session.add(ScrapeTarget(url=url, brand_id=brand_id))
            site["targets_added"] += 1
            added += 1
    session.commit()

    return {"brand": brand_name, "total_found": len(found_urls), "added_to_queue": added, "sites": sites}


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

    # 2. Pending, previously failed and newly uploaded URLs, and the pages re-scraped every run
    pending_targets = bulk_targets(session, brand_id)

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
        # _process_target advances `progress` for every outcome including
        # skips, so the flag is fresh here. The current page is always
        # finished and stamped before stopping.
        if progress and progress.cancel_requested:
            report.cancelled = True
            break

    await _apple_options_after_buy_pages(session, report)
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
        if progress and progress.cancel_requested:
            report.cancelled = True
            break

    await _apple_options_after_buy_pages(session, report)
    return _finalise(report, run_ts)
