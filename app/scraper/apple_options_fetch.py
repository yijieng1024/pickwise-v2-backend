"""
Apple build-to-order options as chip kits (step 10c, decided 2026-11-04).

Runs after the Apple buy page scrape, in the same job, and from
app/scripts/backfill_apple_options.py. Per product, line and size on a stored buy page:

1. The configure page of one standard config per chip it sells (the newest stored
   buy-page raw row lists each configuration's slugs and core counts).
2. The chips that size offers: those its specs page lists. Apple's API names every chip
   of the collection on every size, so the rest are listed in `chip_not_offered` and
   never asked about.
3. Per offered chip, one `update-config` call on a valid kit: the standard config's kit
   when one has that chip, else Apple's own kit for it from one `dependency-switcher`
   call. That response is the chip kit: its price and what the chip allows.

- MacBook Neo is skipped: it has no build-to-order (Jack, 2026-11-04).
- A 403, a 429 or a captcha page stops the whole step (`stopped`): carrying on would
  only make the block worse. Kits not reached keep their stored rows.
- Any other failure (a call failed, a response did not parse) keeps the stored rows of
  the kit (or of every kit of the size, when its configure page failed) and lists it in
  `options_failed`.
- A label that cannot be found is left null and listed; a chip with no label on the page
  gets one from the specs page.

Reads are short and committed before the network part, so no transaction idles across
minutes of HTTP; the write replaces each fetched kit's rows.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from app.laptops.option_model import LaptopOption
from app.processor import apple_options
from app.processor.apple_fields import section_for
from app.processor.apple_name import SPECS_PAGES, SplitFailed, product_and_line, size_sections
from app.scraper.models import RawScrapLaptop

BASE_URL = "https://www.apple.com"
PAUSE_S = 3.0
# Only its two standard configs, no build-to-order (Jack, 2026-11-04): skipped, not failed.
NO_BUILD_TO_ORDER = {"https://www.apple.com/my/shop/buy-mac/macbook-neo"}
_KEY = ("product", "size_inch", "line", "chip")

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
    """One target per product, line and size on a stored buy page, and the ones that
    cannot be fetched (listed as failed)."""
    targets, failed = [], []
    for buy_url, specs_url in SPECS_PAGES.items():
        if buy_url in NO_BUILD_TO_ORDER:
            continue
        raw = _latest_raw(session, buy_url)
        buy = (raw.raw_specs_dump or {}).get("buy_page") if raw else None
        if not buy:
            continue
        product, line = product_and_line(specs_url)
        specs = _latest_raw(session, specs_url)
        features = ((specs.raw_specs_dump or {}).get("scraped_features") or []) if specs else []
        text = "\n".join(f for f in features if isinstance(f, str))
        try:
            sections = size_sections(text, product) if text else []
        except SplitFailed:
            sections = []
        sizes: dict[int, list[dict]] = {}
        for config in buy.get("configs") or []:
            if config.get("size") and config.get("slugs"):
                sizes.setdefault(int(config["size"]), []).append(
                    {"cores": (config.get("cpu_cores"), config.get("gpu_cores")),
                     "url": f"{buy['url']}/{config['slugs'][0]}"})
        for size, standards in sorted(sizes.items()):
            target = {"product": product, "size_inch": size, "line": line or ""}
            section = section_for(sections, size) if sections else None
            if section is None:
                failed.append({**target, "reason": f"no specs page section for {size}-inch"})
            else:
                targets.append({**target, "section": section, "standards": standards})
    return targets, failed


# --------------------------------------------------------------------------- network

def fetch_size(target: Mapping, get: Get, pause: Callable[[], None]) -> dict:
    """Every chip kit of one product, line and size. A configure page failing raises;
    a kit failing is listed and the others carry on."""
    key = {k: target[k] for k in ("product", "size_inch", "line")}

    def fetch(url: str) -> str:
        body = get(url)
        pause()
        return body

    def call(path: str, kit: Mapping[str, str], group: Optional[str] = None, value: Optional[str] = None) -> Any:
        body = fetch(f"{BASE_URL}{path}&{apple_options.query(kit, group, value)}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise apple_options.OptionsParseError(f"not JSON from {path.split('?')[0]}") from exc

    # One configure page per chip a standard config of this size has.
    pages: dict[str, apple_options.ConfigurePage] = {}
    seen: set = set()
    for standard in target["standards"]:
        if tuple(standard["cores"]) in seen:
            continue
        seen.add(tuple(standard["cores"]))
        page = apple_options.parse_configure_page(fetch(standard["url"]))
        pages.setdefault(page.chip, page)
    first = next(iter(pages.values()))
    labels = {k: v for page in pages.values() for k, v in page.labels.items()}

    chips = apple_options.chip_strings(target["section"])
    named = list(dict.fromkeys([*pages, *(v for g, v in labels if g == "chip")]))
    not_offered = [c for c in named if not apple_options.offered(c, chips)]

    kits, failed, labels_missing = [], [], []
    for chip in (c for c in named if apple_options.offered(c, chips)):
        try:
            if chip in pages:
                kit_dims = pages[chip].kit
            else:
                kit_dims = apple_options.switched_kit(call(first.switcher_path, first.kit, "chip", chip), chip)
            kit = apple_options.parse_kit(call(first.update_path, kit_dims))
            if kit.chip != chip:
                raise apple_options.OptionsParseError(f"update-config answered for chip {kit.chip!r}")
        except Blocked:
            raise
        except Exception as exc:  # one kit's failure never stops the others
            failed.append({**key, "chip": chip, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        cores = apple_options.cores_of(chip)
        cpu_model, gpu_model = chips[cores]
        rows = [{"option_group": "chip", "value": chip,
                 "label": labels.get(("chip", chip)) or f"{cores[0]}-core CPU, {cores[1]}-core GPU",
                 "add_on_rm": None, "kit_price_rm": kit.price_rm, "is_included": True, "is_blocked": False,
                 "cpu_model": cpu_model, "gpu_model": gpu_model}]
        for o in kit.options:
            label = labels.get((o.group, o.value))
            if label is None:
                labels_missing.append(f"{chip} {o.group}:{o.value}")
            rows.append({"option_group": o.group, "value": o.value, "label": label, "add_on_rm": o.add_on_rm,
                         "kit_price_rm": None, "is_included": o.is_included, "is_blocked": o.is_blocked,
                         "cpu_model": None, "gpu_model": None})
        kits.append({**key, "chip": chip, "rows": rows})
    return {"kits": kits, "failed": failed, "not_offered": not_offered, "labels_missing": labels_missing}


def fetch_all(targets: list[dict], get: Get = http_get, pause_s: float = PAUSE_S) -> dict:
    calls = 0

    def counted(url: str) -> str:
        nonlocal calls
        calls += 1
        return get(url)

    kits, failed, not_offered, labels_missing, stopped = [], [], [], [], None
    for target in targets:
        key = {k: target[k] for k in ("product", "size_inch", "line")}
        try:
            size = fetch_size(target, counted, lambda: time.sleep(pause_s))
        except Blocked as exc:
            stopped = {**key, "reason": str(exc)}
            break
        except Exception as exc:  # a size's configure page failed: every kit of it keeps its rows
            failed.append({**key, "chip": None, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        kits += size["kits"]
        failed += size["failed"]
        if size["not_offered"]:
            not_offered.append({**key, "chips": size["not_offered"]})
        if size["labels_missing"]:
            labels_missing.append({**key, "options": size["labels_missing"]})
    return {"kits": kits, "options_failed": failed, "chip_not_offered": not_offered,
            "labels_missing": labels_missing, "stopped": stopped, "calls": calls}


# --------------------------------------------------------------------------- write

def write(session: Session, kits: list[dict]) -> None:
    """Replace each fetched kit's rows. Flushes; the caller commits."""
    now = datetime.now(timezone.utc)
    for kit in kits:
        key = {k: kit[k] for k in _KEY}
        session.exec(delete(LaptopOption).where(  # type: ignore[arg-type]
            *(getattr(LaptopOption, k) == v for k, v in key.items())))
        for row in kit["rows"]:
            session.add(LaptopOption(**key, fetched_at=now, **row))
    session.flush()


def summary(targets_failed: list[dict], fetched: dict) -> dict:
    """What a job result and the script print: counts and lists, no option rows."""
    per_group: dict[str, int] = {}
    for kit in fetched["kits"]:
        for row in kit["rows"]:
            per_group[row["option_group"]] = per_group.get(row["option_group"], 0) + 1
    return {
        "chip_kits": len(fetched["kits"]),
        "options_per_group": per_group,
        "chip_not_offered": fetched["chip_not_offered"],
        "labels_missing": fetched["labels_missing"],
        "options_failed": targets_failed + fetched["options_failed"],
        "stopped": fetched["stopped"],
        "apple_calls": fetched["calls"],
    }


async def run_options_step(session: Session, get: Get = http_get, pause_s: float = PAUSE_S) -> dict:
    """The step inside the Apple buy page job: read, fetch (no transaction open), write."""
    targets, targets_failed = read_targets(session)
    session.commit()
    fetched = await asyncio.to_thread(fetch_all, targets, get, pause_s)
    write(session, fetched["kits"])
    session.commit()
    return summary(targets_failed, fetched)
