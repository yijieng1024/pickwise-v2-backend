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

Education prices (step 10d, decided 2026-11-10) are a second pass after that write, on
Apple's education store: the same paths with /my/ as /my-edu/ (`edu_url`). One
configuration page per standard config gives its Edu price (`laptop_edu_prices`), and
the same chip-kit fetch gives each option's Edu add-on (the `edu_` columns of rows that
already exist). The pass never touches an official price, and its failure is reported
under `edu`, never raised.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from app.laptops.laptop_models import Laptop, LaptopEduPrice
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

def apple_brand_ids(session: Session) -> set:
    """The Apple brand row(s): the fetch's lock checks which scrape jobs touch Apple."""
    from app.laptops.brand_model import LaptopBrand

    return {b.id for b in session.exec(select(LaptopBrand)) if b.name.lower() == "apple"}


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
        same_kit = [getattr(LaptopOption, k) == v for k, v in key.items()]
        # The Edu pass runs after this one and may fail: a replaced row keeps its Edu numbers.
        edu = {(o.option_group, o.value): {f: getattr(o, f) for f in _EDU_FIELDS}
               for o in session.exec(select(LaptopOption).where(*same_kit))}
        session.exec(delete(LaptopOption).where(*same_kit))  # type: ignore[arg-type]
        for row in kit["rows"]:
            session.add(LaptopOption(**key, fetched_at=now, **row,
                                     **edu.get((row["option_group"], row["value"]), {})))
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
    """The step inside the Apple buy page job: read, fetch (no transaction open), write.
    Then the same again on the education store, unless Apple stopped the first pass."""
    targets, targets_failed = read_targets(session)
    configs = read_edu_configs(session)
    session.commit()
    fetched = await asyncio.to_thread(fetch_all, targets, get, pause_s)
    write(session, fetched["kits"])
    session.commit()
    result = summary(targets_failed, fetched)
    if fetched["stopped"]:
        result["edu"] = {"skipped": "Apple stopped the options fetch"}
        return result
    try:
        edu = await asyncio.to_thread(fetch_edu, configs, targets, get, pause_s)
        result["edu"] = {**write_edu_prices(session, edu["prices"]),
                         "option_rows": write_edu_options(session, edu["kits"]), "chip_kits": len(edu["kits"]),
                         "prices_failed": edu["prices_failed"], "options_failed": edu["options_failed"],
                         "stopped": edu["stopped"], "apple_calls": edu["calls"]}
        session.commit()
    except Exception as exc:  # the Edu pass is extra: its failure is reported, never the step's
        session.rollback()
        result["edu"] = {"error": f"{type(exc).__name__}: {exc}"}
    return result


# --------------------------------------------------------------------------- education store

EDU_SOURCE = "apple_my_edu"
_EDU_FIELDS = ("edu_add_on_rm", "edu_kit_price_rm", "edu_fetched_at")


def edu_url(url: str) -> str:
    """The same page on Apple's Malaysian education store."""
    return url.replace("apple.com/my/", "apple.com/my-edu/", 1)


def read_edu_configs(session: Session) -> list[dict]:
    """Every standard config on a stored buy page (MacBook Neo too): one colour's page
    (decided 2026-11-10: one colour per configuration) and the part numbers of all of them."""
    configs = []
    for buy_url in SPECS_PAGES:
        raw = _latest_raw(session, buy_url)
        buy = (raw.raw_specs_dump or {}).get("buy_page") if raw else None
        for config in (buy or {}).get("configs") or []:
            if config.get("slugs") and config.get("part_numbers"):
                configs.append({"url": f"{buy['url']}/{config['slugs'][0]}",
                                "part_numbers": list(config["part_numbers"])})
    return configs


def fetch_edu(configs: list[dict], targets: list[dict], get: Get = http_get, pause_s: float = PAUSE_S) -> dict:
    """The Edu price of each standard config, then the Edu chip kits. A page whose part
    number is not one of the config's own is a different machine: failed, not stored."""
    from app.scraper.apple_scraper import parse_detail_page

    calls = 0

    def edu_get(url: str) -> str:
        nonlocal calls
        calls += 1
        return get(edu_url(url))

    prices, failed, stopped = [], [], None
    for config in configs:
        url = edu_url(config["url"])
        try:
            page = parse_detail_page(edu_get(config["url"]))
            time.sleep(pause_s)
            if page["part_number"] not in config["part_numbers"]:
                raise ValueError(f"part number {page['part_number']} is not this configuration's")
            prices.append({"part_numbers": config["part_numbers"], "price_rm": page["price_rm"], "url": url})
        except Blocked as exc:
            stopped = {"url": url, "reason": str(exc)}
            break
        except Exception as exc:
            failed.append({"url": url, "reason": f"{type(exc).__name__}: {exc}"})
    kits = {"kits": [], "options_failed": [], "stopped": None}
    if stopped is None:
        kits = fetch_all(targets, edu_get, pause_s)
    return {"prices": prices, "prices_failed": failed, "kits": kits["kits"],
            "options_failed": kits["options_failed"], "stopped": stopped or kits["stopped"], "calls": calls}


def write_edu_prices(session: Session, prices: list[dict]) -> dict:
    """Upsert laptop_edu_prices by part number. Flushes; the caller commits. A price not
    below the official one is stored as Apple states it and listed: the read side
    (options_service.edu_row) is what never shows it."""
    now = datetime.now(timezone.utc)
    by_part = {part: laptop for laptop in session.exec(
        select(Laptop).where(Laptop.part_numbers.isnot(None)))  # type: ignore[union-attr]
        for part in laptop.part_numbers or []}
    written, no_laptop, not_below = 0, [], []
    for price in prices:
        laptop = next((by_part[p] for p in price["part_numbers"] if p in by_part), None)
        if laptop is None:
            no_laptop.append(price["url"])
            continue
        row = session.exec(select(LaptopEduPrice).where(LaptopEduPrice.laptop_id == laptop.id)).first()
        if row is None:
            row = LaptopEduPrice(laptop_id=laptop.id, price_rm=price["price_rm"], source=EDU_SOURCE,
                                 price_changed_at=now)
        elif row.price_rm != price["price_rm"]:
            row.price_rm, row.price_changed_at = price["price_rm"], now
        row.source, row.source_url, row.checked_at = EDU_SOURCE, price["url"], now
        session.add(row)
        written += 1
        if laptop.price_rm and price["price_rm"] >= laptop.price_rm:
            not_below.append({"laptop_id": str(laptop.id), "edu": price["price_rm"], "official": laptop.price_rm})
    session.flush()
    return {"prices": written, "no_laptop": no_laptop, "not_below_official": not_below}


def write_edu_options(session: Session, kits: list[dict]) -> int:
    """Fill the Edu columns of the option rows that exist; a row only the education store
    has is left out. Flushes; the caller commits."""
    now = datetime.now(timezone.utc)
    filled = 0
    for kit in kits:
        stored = {(o.option_group, o.value): o for o in session.exec(
            select(LaptopOption).where(*(getattr(LaptopOption, k) == kit[k] for k in _KEY)))}
        for row in kit["rows"]:
            option = stored.get((row["option_group"], row["value"]))
            if option is None:
                continue
            option.edu_add_on_rm, option.edu_kit_price_rm, option.edu_fetched_at = (
                row["add_on_rm"], row["kit_price_rm"], now)
            session.add(option)
            filled += 1
    session.flush()
    return filled
