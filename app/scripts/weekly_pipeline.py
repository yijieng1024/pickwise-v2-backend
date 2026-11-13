"""
The weekly pipeline (plan step 11, items 8 to 10): one run of every step, in order.

    python -m app.scripts.weekly_pipeline                      # dry run (default): writes nothing
    python -m app.scripts.weekly_pipeline --limit 5            # dry run, 5 re-scraped pages per site
    python -m app.scripts.weekly_pipeline --apply              # a manual run
    python -m app.scripts.weekly_pipeline --apply --scheduled  # what the daily workflow runs

--scheduled reads the settings (Admin, `pipeline_settings`) first and exits at once,
with no job record, when the pipeline is off or today in Malaysia is not its weekday.
Without it (a manual run) both are ignored. The settings' site switches always apply.

Steps, in order, each one the service function the admin's buttons call:
  lock        another scrape or processing job active: the run is skipped
  crawl       feed crawl of each brand with a site switched on, queueing only those sites
  scrape      bulk scrape of the same sites
  process     the AI processor over every pending raw row (it runs the go-live check too)
  guides      apply of the latest guide of each kind
  passmark    PassMark CPU and GPU refresh
  go_live     go-live re-check of the held laptops, with this run's benchmarks
  embeddings  laptops the processor wrote, and laptops with no embedding
  pick_scores generate-all, only if a laptop went live or a price changed

One job record (`pipeline.weekly`) holds the whole run: started_by, runs_outside_render
and every step's result. An item that fails (one brand's crawl, one guide, one list) is
recorded and the run goes on; a step that fails as a whole stops the run, and the
result names it and the steps not run.

The dry run reads the database in a read-only transaction, calls no LLM and fetches only
what app/scripts/rescrape_dry_run.py fetches (the re-scraped ROG and HP pages); it prints
what each step would do.

Reads TEST_DATABASE_URL unless WEEKLY_PIPELINE_TARGET=production is set, and checks that
the URL is the database it claims to be (app/common/db_target.py).
"""
import argparse
import asyncio
import collections
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlmodel import Session, select

from app.common.db_target import host_of, write_target
from app.common.job_model import BackgroundJob, JobStarter, JobStatus, JobType
from app.pipeline.settings import WEEKDAYS, PipelineSettings, read_settings

MALAYSIA = ZoneInfo("Asia/Kuala_Lumpur")
# The processor's own ceiling per run; a week's pages are far below it.
PROCESS_LIMIT = 1500


# ---------------------------------------------------------------------------
# Whether a scheduled run goes ahead
# ---------------------------------------------------------------------------

def not_due(settings: PipelineSettings, now: datetime, scheduled: bool) -> Optional[str]:
    """Why a run exits before doing anything, or None when it goes ahead. Only a
    scheduled run reads on/off and the weekday; a manual run always goes ahead."""
    if not scheduled:
        return None
    if not settings.enabled:
        return "the pipeline is switched off in admin"
    today = WEEKDAYS[now.astimezone(MALAYSIA).weekday()]
    if today != settings.weekday:
        return f"today is {today} in Malaysia, and the pipeline runs on {settings.weekday}"
    return None


# ---------------------------------------------------------------------------
# The steps of an --apply run
# ---------------------------------------------------------------------------

@dataclass
class Run:
    session: Session
    settings: PipelineSettings
    started_at: datetime
    results: dict = field(default_factory=dict)


class _NoProgress:
    """What PassMark's refresh reports to when the pipeline's job counts steps."""
    cancel_requested = False

    def set_total(self, total: int) -> None:
        pass

    def advance(self, **_: Any) -> None:
        pass


def _brand_id(session: Session, name: str):
    from app.laptops.brand_model import LaptopBrand
    brand = session.exec(select(LaptopBrand).where(func.lower(LaptopBrand.name) == name)).first()
    if brand is None:
        raise ValueError(f"no laptop_brands row named {name!r}")
    return brand.id


def _each(run: Run, items, do: Callable[[Any], dict]) -> dict:
    """*do* for each item; one that raises is recorded as that item's error."""
    out = {}
    for key, item in items:
        try:
            out[key] = do(item)
        except Exception as e:
            run.session.rollback()
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    return out


def step_crawl(run: Run) -> dict:
    from app.scraper.bulk_scraper import run_feed_crawl
    sites = run.settings.sites()
    return _each(run, [(brand, (_brand_id(run.session, brand), hosts)) for brand, hosts in sites.items()],
                 lambda b: asyncio.run(run_feed_crawl(b[0], run.session, hosts=b[1])))


def step_scrape(run: Run) -> dict:
    from app.scraper.bulk_scraper import run_bulk_scrape

    def scrape(item) -> dict:
        report = asyncio.run(run_bulk_scrape(item[0], run.session, hosts=item[1]))
        return {"pages": report.total_pending, "succeeded": report.succeeded, "failed": report.failed,
                "skipped": report.skipped,
                "failed_pages": [{"url": r.url, "error": r.error} for r in report.results if r.status == "failed"]}

    sites = run.settings.sites()
    return _each(run, [(brand, (_brand_id(run.session, brand), hosts)) for brand, hosts in sites.items()], scrape)


def step_process(run: Run) -> dict:
    from app.processor.engine import process_pending_laptops
    result = process_pending_laptops(run.session, limit=PROCESS_LIMIT)
    result.pop("details", None)  # per-record copies of the lists kept below
    return result


def step_guides(run: Run) -> dict:
    from app.laptops.price_guide_model import PriceGuide, PriceGuideKind
    from app.laptops.price_guide_service import apply_guide
    latest = []
    for kind in PriceGuideKind:
        guide = run.session.exec(
            select(PriceGuide).where(PriceGuide.kind == kind.value)
            .order_by(PriceGuide.uploaded_at.desc(), PriceGuide.id)  # type: ignore[attr-defined]
        ).first()
        if guide is not None:
            latest.append((kind.value, guide.id))
    run.session.commit()
    return _each(run, latest, lambda guide_id: apply_guide(run.session, guide_id))


def step_passmark(run: Run) -> dict:
    from app.benchmark.passmark_scraper import refresh
    return _each(run, [("cpu", "cpu"), ("gpu", "gpu")], lambda source: refresh(source, run.session, _NoProgress()))


def step_go_live(run: Run) -> dict:
    from app.laptops.go_live import run_go_live_check
    return run_go_live_check(run.session)


def embedding_ids(session: Session, process_result: Optional[dict]) -> list:
    """New laptops and laptops whose page changed: every laptop the processor wrote
    (it is sent only new or changed pages), and every laptop with no embedding."""
    from app.laptops.laptop_models import Laptop, LaptopEmbedding
    written = {w["laptop_id"] for w in (process_result or {}).get("writes", [])
               if w.get("laptop_id") and w["action"] in ("inserted", "updated")}
    missing = session.exec(
        select(Laptop.id).outerjoin(LaptopEmbedding, LaptopEmbedding.laptop_id == Laptop.id)
        .where(LaptopEmbedding.id.is_(None))  # type: ignore[union-attr]
    ).all()
    return sorted(written | {str(i) for i in missing})


def step_embeddings(run: Run) -> dict:
    from uuid import UUID
    from app.embeddings.service import generate_single_laptop_embedding
    ids = embedding_ids(run.session, run.results.get("process"))
    run.session.commit()
    failed = []
    for laptop_id in ids:
        result = generate_single_laptop_embedding(run.session, UUID(laptop_id))
        if result.get("status") != "success":
            run.session.rollback()
            failed.append({"laptop_id": laptop_id, "error": result.get("message")})
    return {"embedded": len(ids) - len(failed), "failed": failed}


def went_live(results: dict) -> list[dict]:
    """Every laptop this run made active: at the end of processing, and in the re-check."""
    return [*((results.get("process") or {}).get("go_live") or {}).get("activated", []),
            *(results.get("go_live") or {}).get("activated", [])]


def price_changes(session: Session, since: datetime) -> list[dict]:
    """Every price written since *since* that differs from the laptop's price before it:
    the processor's and the guide apply's, read from laptop_price_history, where both
    record a change. A laptop's first price is not a change."""
    from app.laptops.laptop_models import Laptop, LaptopPriceHistory
    rows = session.exec(
        select(LaptopPriceHistory, Laptop.model_code, Laptop.product_name)
        .join(Laptop, Laptop.id == LaptopPriceHistory.laptop_id)
        .where(LaptopPriceHistory.recorded_at >= since)
        .order_by(LaptopPriceHistory.recorded_at, LaptopPriceHistory.id)  # type: ignore[arg-type]
    ).all()
    changes = []
    for row, model_code, product_name in rows:
        before = session.exec(
            select(LaptopPriceHistory.price_rm).where(
                LaptopPriceHistory.laptop_id == row.laptop_id, LaptopPriceHistory.recorded_at < row.recorded_at)
            .order_by(LaptopPriceHistory.recorded_at.desc())  # type: ignore[attr-defined]
        ).first()
        if before is not None and before != row.price_rm:
            changes.append({"laptop_id": str(row.laptop_id), "model_code": model_code, "product_name": product_name,
                            "old_price": before, "new_price": row.price_rm, "source": row.source})
    return changes


def step_pick_scores(run: Run, generate: Optional[Callable[[Session], dict]] = None) -> dict:
    live, changed = went_live(run.results), price_changes(run.session, run.started_at)
    run.session.commit()
    if not live and not changed:
        return {"skipped": "no laptop went live and no price changed"}
    if generate is None:
        from app.laptops.pickscore_general import generate_all_pick_scores as generate
    return generate(run.session)


STEPS: tuple[tuple[str, Callable[[Run], dict]], ...] = (
    ("crawl", step_crawl),
    ("scrape", step_scrape),
    ("process", step_process),
    ("guides", step_guides),
    ("passmark", step_passmark),
    ("go_live", step_go_live),
    ("embeddings", step_embeddings),
    ("pick_scores", step_pick_scores),
)


def run_steps(run: Run, steps=STEPS, on_step: Optional[Callable[[str], None]] = None) -> dict:
    """Each step in order. One that raises stops the run: the result names it, its
    error, and the steps after it, which did not run."""
    run.results = {}
    for i, (name, step) in enumerate(steps):
        try:
            run.results[name] = step(run)
        except Exception as e:
            run.session.rollback()
            return {"steps": run.results, "stopped_at": name, "error": f"{type(e).__name__}: {e}",
                    "not_run": [later for later, _ in steps[i + 1:]]}
        if on_step:
            on_step(name)
    return {"steps": run.results, "stopped_at": None}


def busy_job(session: Session) -> Optional[str]:
    """The id of an active scrape or processing job, which skips the run; None if free.
    The lock check the admin's buttons use, dead-worker rule included."""
    from app.common.job_service import refuse_if_running
    try:
        refuse_if_running(session, JobType.SCRAPE_AND_PROCESS, "another scrape or processing job is active")
    except HTTPException as refused:
        return refused.detail["job_id"]
    return None


def apply(engine, settings: PipelineSettings, scheduled: bool) -> dict:
    """An --apply run under one job record. Returns the job's result."""
    from app.common.job_service import JobProgress, create_job

    with Session(engine) as session:
        started_at = datetime.now(timezone.utc)
        busy = busy_job(session)
        job = create_job(
            session, job_type=JobType.WEEKLY_PIPELINE, total_count=0 if busy else len(STEPS),
            params={"scheduled": scheduled, "sites": settings.sites()},
            started_by=JobStarter.SCHEDULE if scheduled else JobStarter.ADMIN, runs_outside_render=True,
        )
        progress = JobProgress(job.id)
        progress.mark_processing()
        try:
            if busy:
                result = {"skipped": f"another scrape or processing job is active (job {busy})"}
            else:
                result = run_steps(Run(session, settings, started_at), on_step=lambda name: progress.advance(item=name))
                result["went_live"] = went_live(result["steps"])
                result["price_changes"] = price_changes(session, started_at)
                session.commit()
        except Exception as e:
            progress.mark_failed(f"{type(e).__name__}: {e}")
            raise
        if result.get("stopped_at"):
            progress.mark_failed(f"stopped at {result['stopped_at']}: {result['error']}", result)
        else:
            progress.mark_completed(result)
        return {"job_id": str(job.id), **result}


# ---------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------

def dry_run(session: Session, settings: PipelineSettings, limit: Optional[int]) -> dict:
    """What each step would do, printed as it goes. Reads only; the caller's transaction
    is read-only, so a write anywhere below fails instead of happening."""
    from app.benchmark.model import CPUBenchmark, GPUBenchmark
    from app.laptops.go_live import failed_conditions
    from app.laptops.laptop_models import Laptop, LaptopStatus
    from app.laptops.price_guide_model import PriceGuide, PriceGuideKind
    from app.laptops.price_guide_service import preview
    from app.scraper.bulk_scraper import bulk_targets
    from app.scraper.models import RawScrapLaptop
    from app.scripts import rescrape_dry_run as rescrape

    sites = settings.sites()
    hosts = {h for hs in sites.values() for h in hs}
    out: dict = {"steps": {}}

    active = session.exec(
        select(BackgroundJob).where(BackgroundJob.job_type.in_(JobType.SCRAPE_AND_PROCESS))  # type: ignore[attr-defined]
        .where(BackgroundJob.status.in_(JobStatus.ACTIVE))  # type: ignore[attr-defined]
    ).first()
    print(f"lock        {'BUSY: job ' + str(active.id) + ' (' + active.job_type + '), the run would be skipped' if active else 'free'}")
    if active:
        out["skipped"] = f"another scrape or processing job is active (job {active.id})"

    print("crawl       " + "; ".join(f"{b} ({', '.join(h)})" for b, h in sites.items())
          + ". Not fetched in a dry run")

    queued = {}
    for brand, brand_hosts in sites.items():
        targets = [t for t in bulk_targets(session, _brand_id(session, brand))
                   if t.url.split("/")[2].lower() in brand_hosts]
        queued[brand] = len(targets)
    print("scrape      " + ", ".join(f"{b} {n} pages" for b, n in queued.items())
          + " (already queued; the crawl may add more)")

    connection = session.connection()
    targets = [t for t in rescrape.read_targets(connection) if t.url.split("/")[2].lower() in hosts]
    if limit:
        by_site: dict = collections.defaultdict(list)
        for t in targets:
            by_site[t.url.split("/")[2].lower()].append(t)
        targets = [t for site in by_site.values() for t in site[:limit]]
    stored, laptops = rescrape.read_stored(connection), rescrape.read_laptops(connection)
    variants, failures = asyncio.run(rescrape.fetch(targets, stored))
    rows = rescrape.plan(variants, laptops)
    counts = collections.Counter(r["outcome"] for r in rows)
    print(f"            re-scrape of {len(targets)} ROG/HP pages: {len(rows)} variants, {len(failures)} failed fetches | "
          + ", ".join(f"{k} {counts[k]}" for k in ("unchanged", "no SKU", "SKU", "attach", "insert", "slug")))
    out["steps"]["scrape"] = {"queued": queued, "rescrape": dict(counts),
                              "failed_pages": [{"url": u, "error": e} for u, e in failures]}

    pending = session.exec(
        select(func.count()).select_from(RawScrapLaptop).where(RawScrapLaptop.processing_status.in_(("pending", "failed")))
    ).one()
    to_process = sum(counts[k] for k in ("SKU", "attach", "insert", "slug"))
    print(f"process     {pending} raw rows pending or failed now, plus {to_process} re-scraped variants that changed"
          f" ({counts['insert']} would insert, {counts['attach']} attach a SKU). No LLM in a dry run")
    out["steps"]["process"] = {
        "pending_now": pending, "rescraped_changed": to_process,
        "sku_price_differences": [
            {"laptop_id": str(d["laptop"].id), "model_code": d["laptop"].model_code, "kept_price": d["kept"],
             "other_skus": d["prices"]} for d in rescrape.price_differences(rows)],
    }

    fills: list[dict] = []
    guides = {}
    for kind in PriceGuideKind:
        guide = session.exec(select(PriceGuide).where(PriceGuide.kind == kind.value)
                             .order_by(PriceGuide.uploaded_at.desc(), PriceGuide.id)).first()  # type: ignore[attr-defined]
        if guide is None:
            continue
        p = preview(session, guide)
        fill = [m for m in p["matched"] if m["action"] == "fill"]
        fills += [{"laptop_id": m["id"], "model_code": m["model_code"], "product_name": m["product_name"],
                   "old_price": m["price_rm"], "new_price": m["guide_price_rm"], "source": "guide"}
                  for m in fill if m["price_rm"]]
        guides[kind.value] = {"guide": guide.filename, "fill": len(fill), **p["counts"]}
        print(f"guides      {kind.value}: {guide.filename} ({guide.uploaded_at:%Y-%m-%d}) would fill {len(fill)},"
              f" {p['counts']['differs_from_guide']} differ, {p['counts']['ambiguous'] + p['counts']['conflict']} unclear")
    out["steps"]["guides"] = guides
    out["price_changes"] = fills

    print("passmark    would refresh the CPU and GPU lists. Not fetched in a dry run")

    held = session.exec(select(Laptop).where(Laptop.go_live_held == True)  # noqa: E712
                        .where(Laptop.status == LaptopStatus.INACTIVE.value)).all()
    cpu = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]
    go = {"activated": [], "held": [], "no_family": []}
    for laptop in held:
        entry = {"laptop_id": str(laptop.id), "model_code": laptop.model_code, "product_name": laptop.product_name}
        failed = failed_conditions(laptop, 0, cpu, gpu)
        (go["held"].append({**entry, "failed": failed}) if failed else go["activated"].append(entry))
    print(f"go_live     {len(held)} held now: {len(go['activated'])} would go live, {len(go['held'])} stay held"
          " (before this run's new laptops and benchmarks)")
    out["steps"]["go_live"] = go
    out["went_live"] = go["activated"]

    missing = len(embedding_ids(session, None))
    print(f"embeddings  {missing} laptops have no embedding, plus every laptop the processor writes")

    if go["activated"] or fills:
        print(f"pick_scores would run: {len(go['activated'])} laptops go live, {len(fills)} guide prices change")
    else:
        print("pick_scores runs only if the processing changes a price or a new laptop goes live")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="do the run (default: dry run, writes nothing)")
    parser.add_argument("--scheduled", action="store_true", help="exit unless switched on and today is the weekday")
    parser.add_argument("--limit", type=int, help="dry run only: the first N re-scraped pages of each site")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    url = write_target("WEEKLY_PIPELINE_TARGET")
    # Every service reads app.database.engine, built from settings.database_url on first
    # use: point it at the checked URL before anything touches it.
    os.environ["DATABASE_URL"] = url
    from app.database import engine

    print(f"{'run' if args.apply else 'dry run'} against {host_of(url)}"
          f"{'' if args.apply else '; nothing is written'}")
    with Session(engine) as session:
        settings = read_settings(session)
        session.expunge_all()
    reason = not_due(settings, datetime.now(timezone.utc), args.scheduled)
    if reason:
        print(f"not today: {reason}. Exiting with no job record")
        return 0

    if args.apply:
        result = apply(engine, settings, args.scheduled)
        print(f"job {result['job_id']}: " + (result.get("skipped") or (
            f"stopped at {result['stopped_at']}" if result.get("stopped_at") else "completed")))
        return 1 if result.get("stopped_at") else 0

    with Session(engine) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        dry_run(session, settings, args.limit)
        session.rollback()
    return 0


if __name__ == "__main__":
    sys.exit(main())
