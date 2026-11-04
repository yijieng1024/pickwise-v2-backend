"""
Apple build-to-order options for every standard config (step 10c, decided 2026-11-04).

Runs after the Apple buy page scrape, in the same job, and from
app/scripts/backfill_apple_options.py. Per standard config (an Apple row with part
numbers, active or inactive): its configure page, one `update-config` call, and one
`dependency-switcher` call per option that can force a change (each listed one the
config does not have, and each one the page shows but update-config does not list,
which the switcher also prices), a few seconds apart.

- The configure page is found through the newest stored buy-page raw row, which lists
  each configuration's slugs and part numbers in the same order.
- A 403, a 429 or a captcha page stops the whole step (`stopped`): carrying on would
  only make the block worse. Machines not reached keep their stored options.
- Any other failure for one machine (a call failed, a response did not parse) keeps
  that machine's stored options and lists it in `options_failed`.
- MacBook Neo is skipped: it has no build-to-order (Jack, 2026-11-04).
- Apple lists every chip of the collection on every size, so a chip option is kept only
  if the size's specs page lists it; the rest, and their rules, are dropped and listed in
  `chip_not_offered` (decided 2026-11-04). A label that cannot be found is left null and
  listed; a chip with no label on the page gets one from the specs page.
- An option reached only through a forced change is priced at its own share
  (`apple_options.own_shares`), so add-ons sum.

Reads are short and committed before the network part, so no transaction idles
across minutes of HTTP; the write replaces each fetched machine's rows.
"""
import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.laptops.option_model import LaptopOption, LaptopOptionRule
from app.processor import apple_options
from app.processor.apple_fields import section_for
from app.processor.apple_name import SPECS_PAGES, SplitFailed, product_and_line, size_sections
from app.scraper.models import RawScrapLaptop

BASE_URL = "https://www.apple.com"
PAUSE_S = 3.0
_FETCHED_STATUSES = (LaptopStatus.ACTIVE.value, LaptopStatus.INACTIVE.value)
# Only its two standard configs, no build-to-order (Jack, 2026-11-04): skipped, not failed.
NO_BUILD_TO_ORDER = {"https://www.apple.com/my/shop/buy-mac/macbook-neo"}

Get = Callable[[str], str]


class Blocked(RuntimeError):
    """Apple refused (403, 429 or a captcha page). The step stops."""


def http_get(url: str) -> str:
    from scrapling.fetchers import Fetcher  # imported here: the unit tier never fetches

    page = Fetcher.get(url, impersonate="chrome", stealthy_headers=True, timeout=30)
    body = page.body.decode("utf-8", "replace")
    if page.status in (403, 429):
        raise Blocked(f"HTTP {page.status} from {url}")
    if page.status != 200:
        raise RuntimeError(f"HTTP {page.status} from {url}")
    if "captcha" in body[:20000].lower():
        raise Blocked(f"captcha page from {url}")
    return body


# --------------------------------------------------------------------------- read

def _latest_raw(session: Session, url: str) -> Optional[RawScrapLaptop]:
    return session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.source_url == url)
        .order_by(RawScrapLaptop.created_at.desc())  # type: ignore[attr-defined]
    ).first()


def read_targets(session: Session) -> tuple[list[dict], list[dict]]:
    """The standard configs to fetch, and the ones that cannot be (listed as failed)."""
    pages: dict[str, dict] = {}    # part number -> {url, size, section}
    no_options: set[str] = set()   # part numbers on a buy page with no build-to-order
    for buy_url, specs_url in SPECS_PAGES.items():
        raw = _latest_raw(session, buy_url)
        buy = (raw.raw_specs_dump or {}).get("buy_page") if raw else None
        if not buy:
            continue
        if buy_url in NO_BUILD_TO_ORDER:
            no_options.update(p for c in buy.get("configs") or [] for p in c.get("part_numbers") or [])
            continue
        specs = _latest_raw(session, specs_url)
        features = ((specs.raw_specs_dump or {}).get("scraped_features") or []) if specs else []
        text = "\n".join(f for f in features if isinstance(f, str))
        try:
            sections = size_sections(text, product_and_line(specs_url)[0]) if text else []
        except SplitFailed:
            sections = []
        for config in buy.get("configs") or []:
            # Neo's buy page states no size; its specs page is one section anyway.
            section = section_for(sections, config.get("size") or 0) if sections else None
            for slug, part in zip(config.get("slugs") or [], config.get("part_numbers") or []):
                pages[part] = {"url": f"{buy['url']}/{slug}", "size": config.get("size"), "section": section}

    rows = session.exec(
        select(Laptop.id, Laptop.model_code, Laptop.status, Laptop.part_numbers)  # type: ignore[call-overload]
        .join(LaptopBrand, LaptopBrand.id == Laptop.brand_id)
        .where(func.lower(LaptopBrand.name) == "apple", Laptop.status.in_(_FETCHED_STATUSES))  # type: ignore[attr-defined]
        .order_by(Laptop.model_code)
    ).all()
    targets, failed = [], []
    for laptop_id, model_code, status, parts in rows:
        if not parts or any(p in no_options for p in parts):
            continue  # build-to-order rows (no standard config of their own), and Neo
        page = next((pages[p] for p in parts if p in pages), None)
        entry = {"laptop_id": str(laptop_id), "model_code": model_code, "status": status}
        if page is None:
            failed.append({**entry, "reason": "none of its part numbers is on a stored buy page"})
        else:
            targets.append({**entry, **page})
    return targets, failed


# --------------------------------------------------------------------------- network

def fetch_machine(target: Mapping, get: Get, pause: Callable[[], None]) -> dict:
    """One standard config's options and rules, ready to write. Raises on any failure."""
    page = apple_options.parse_configure_page(get(target["url"]))
    pause()

    def call(path: str, group: Optional[str] = None, value: Optional[str] = None) -> Any:
        body = get(f"{BASE_URL}{path}&{apple_options.query(page.kit, group, value)}")
        pause()
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise apple_options.OptionsParseError(f"not JSON from {path.split('?')[0]}") from exc

    # Chips this size really has, from its specs page: the rest are not asked about at all.
    chips = apple_options.chip_strings(target.get("section"))

    def is_offered(group: str, value: str) -> bool:
        return apple_options.offered(group, value, chips)

    listed = apple_options.parse_update_config(call(page.update_path))
    if any(o.is_included and not is_offered(o.group, o.value) for o in listed):
        raise apple_options.OptionsParseError("its own chip is not on its size's specs page")
    unlisted = apple_options.unlisted(listed, page)
    not_offered = sorted({o.value for o in listed if not is_offered(o.group, o.value)}
                         | {v for g, v in unlisted if not is_offered(g, v)})
    options = [o for o in listed if is_offered(o.group, o.value)]

    rules = []
    for option in apple_options.triggers(options):
        rules += apple_options.parse_dependency_switcher(
            call(page.switcher_path, option.group, option.value), option.group, option.value).rules
    switches = {}
    for group, value in unlisted:
        if is_offered(group, value):
            switches[(group, value)] = apple_options.parse_dependency_switcher(
                call(page.switcher_path, group, value), group, value)
            rules += switches[(group, value)].rules
    options += apple_options.own_shares(options, switches)
    rules = [r for r in dict.fromkeys(rules)
             if is_offered(r.trigger_group, r.trigger_value) and is_offered(r.forced_group, r.forced_value)]

    rows, labels_missing = [], []
    for o in options:
        cpu_model = gpu_model = None
        label = page.labels.get((o.group, o.value))
        if o.group == "chip":
            cores = apple_options.cores_of(o.value)
            cpu_model, gpu_model = chips[cores]
            label = label or f"{cores[0]}-core CPU, {cores[1]}-core GPU"  # the Pro 14's M6 has none
        if label is None:
            labels_missing.append(f"{o.group}:{o.value}")
        rows.append({"option_group": o.group, "value": o.value, "label": label, "add_on_rm": o.add_on_rm,
                     "is_included": o.is_included, "is_blocked": o.is_blocked,
                     "cpu_model": cpu_model, "gpu_model": gpu_model})
    return {"options": rows, "rules": [asdict(r) for r in rules],
            "labels_missing": labels_missing, "chip_not_offered": not_offered}


def fetch_all(targets: list[dict], get: Get = http_get, pause_s: float = PAUSE_S) -> dict:
    calls = 0

    def counted(url: str) -> str:
        nonlocal calls
        calls += 1
        return get(url)

    machines, failed, stopped = {}, [], None
    for target in targets:
        entry = {k: target[k] for k in ("laptop_id", "model_code", "status")}
        try:
            machines[target["laptop_id"]] = {**entry, **fetch_machine(target, counted, lambda: time.sleep(pause_s))}
        except Blocked as exc:
            stopped = {**entry, "reason": str(exc)}
            break
        except Exception as exc:  # one machine's failure never stops the others
            failed.append({**entry, "reason": f"{type(exc).__name__}: {exc}"})
    return {"machines": machines, "options_failed": failed, "stopped": stopped, "calls": calls}


# --------------------------------------------------------------------------- write

def write(session: Session, machines: Mapping[str, dict]) -> None:
    """Replace each fetched machine's options and rules. Flushes; the caller commits."""
    now = datetime.now(timezone.utc)
    for laptop_id, machine in machines.items():
        session.exec(delete(LaptopOption).where(LaptopOption.laptop_id == laptop_id))  # type: ignore[arg-type]
        session.exec(delete(LaptopOptionRule).where(LaptopOptionRule.laptop_id == laptop_id))  # type: ignore[arg-type]
        for row in machine["options"]:
            session.add(LaptopOption(laptop_id=laptop_id, fetched_at=now, **row))
        for rule in machine["rules"]:
            session.add(LaptopOptionRule(laptop_id=laptop_id, fetched_at=now, **rule))
    session.flush()


def summary(targets_failed: list[dict], fetched: dict) -> dict:
    """What a job result and the script print: counts and lists, no option rows."""
    machines = fetched["machines"].values()
    per_group: dict[str, int] = {}
    for m in machines:
        for o in m["options"]:
            per_group[o["option_group"]] = per_group.get(o["option_group"], 0) + 1
    return {
        "machines": len(fetched["machines"]),
        "options_per_group": per_group,
        "rules": sum(len(m["rules"]) for m in machines),
        "labels_missing": [{"model_code": m["model_code"], "options": m["labels_missing"]}
                           for m in machines if m["labels_missing"]],
        "chip_not_offered": [{"model_code": m["model_code"], "chips": m["chip_not_offered"]}
                             for m in machines if m["chip_not_offered"]],
        "options_failed": targets_failed + fetched["options_failed"],
        "stopped": fetched["stopped"],
        "apple_calls": fetched["calls"],
    }


async def run_options_step(session: Session, get: Get = http_get, pause_s: float = PAUSE_S) -> dict:
    """The step inside the Apple buy page job: read, fetch (no transaction open), write."""
    targets, targets_failed = read_targets(session)
    session.commit()
    fetched = await asyncio.to_thread(fetch_all, targets, get, pause_s)
    write(session, fetched["machines"])
    session.commit()
    return summary(targets_failed, fetched)
