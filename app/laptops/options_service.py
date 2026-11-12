"""
The configurator behind GET /laptops/{id}/options and /configure (step 10c, step (d)).

A laptop reaches its chip kits by its own product, size and line (from its model_code)
and its chip by its core counts. A configured price is the chip kit's price plus the
chosen options' add-ons on that kit, which is the standard price + the chip's add-on
(kit price - standard price) + the options' add-ons.

The live Pick Score scores the standard row's numbers with RAM, storage, CPU, GPU and
price replaced, as a ScorableProduct built in memory: nothing is saved, and the catalog
ranges it is scored against are the cached ones, built from active rows only, so a
configured machine never enters them. Unresolved benchmarks follow the engine's rule, as
on the detail page: one unresolved part is a flagged neutral 50, both withhold the score
(null, with the reason).

Education prices (step 10d, decided 2026-11-10): `edu=True` builds the same configurator
from the education store's numbers (the `edu_` columns, the laptop's laptop_edu_prices
row), and `edu_view` is the standard config at its Edu price, for any brand. Both score
live with the price replaced and save nothing. This module and its router are the only
readers of an Edu price: Pico's tool calls `configurable` without `edu`, and
tests/unit/test_edu_price_guard.py fails if anything else names one.
"""
import dataclasses
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEduPrice, LaptopStatus
from app.laptops.option_model import LaptopOption
from app.laptops.pickscore_adapter import get_scoring_data, laptop_to_scorable
from app.laptops.pickscore_general import USE_CASE_PRIORITIES
from app.pickscore.engine import calculate_pick_score
from app.processor import apple_options
from app.processor.apple_name import line_of_code

GROUPS = ("memory", "storage", "adapter", "display", "keyboard")
_URL_GROUPS = GROUPS[:4]   # what a configuration's URL can carry, in its fixed order after chip
_CODE = re.compile(r"^apple-macbook-(air|pro)-(\d+)-")
_SIZE = re.compile(r"(\d+)(gb|tb)")


class NotConfigurable(LookupError):
    """Not an active Apple standard config with stored chip kits."""


class NoEduPrice(LookupError):
    """Configurable, but the education store's numbers for it are not stored."""


class NotAllowed(ValueError):
    """A choice the chosen chip does not allow: what it allows and which chips offer it."""

    def __init__(self, message: str, problems: list[dict]):
        super().__init__(message)
        self.message, self.problems = message, problems


@dataclass
class Configurable:
    laptop: Laptop
    rows: list[dict]
    kits: dict[str, apple_options.Kit]
    chip: str                       # the standard config's own chip
    standard: dict[str, str]        # group -> the standard config's own value
    standard_price_rm: float        # the laptop's price: official, or its Edu price
    edu: bool = False


def _value(gb: int) -> str:
    return f"{gb // 1024}tb" if gb >= 1024 and gb % 1024 == 0 else f"{gb}gb"


def _gb(value: str) -> int:
    match = _SIZE.fullmatch(value)
    return int(match.group(1)) * (1024 if match.group(2) == "tb" else 1)


def _included(kit: apple_options.Kit, group: str) -> Optional[str]:
    return next((o.value for o in kit.options if o.group == group and o.is_included), None)


def edu_row(session: Session, laptop: Laptop) -> Optional[LaptopEduPrice]:
    """The laptop's Edu price row, only when it can be shown: an active laptop with an
    official price, and an Edu price below it."""
    if laptop.status != LaptopStatus.ACTIVE.value or not laptop.price_rm:
        return None
    row = session.exec(select(LaptopEduPrice).where(LaptopEduPrice.laptop_id == laptop.id)).first()
    return row if row is not None and 0 < row.price_rm < laptop.price_rm else None


def edu_rows(rows: list[dict]) -> list[dict]:
    """The option rows as the education store prices them. A chip kit with any row the
    Edu pass never filled is left out whole: a total never mixes the two stores."""
    whole = {r["chip"] for r in rows} - {r["chip"] for r in rows if r.get("edu_fetched_at") is None}
    return [{**r, "add_on_rm": r["edu_add_on_rm"], "kit_price_rm": r["edu_kit_price_rm"]}
            for r in rows if r["chip"] in whole]


def configurable(session: Session, laptop_id: uuid.UUID, edu: bool = False) -> Configurable:
    """edu: priced from the education store. Raises NoEduPrice when the laptop is
    configurable but its Edu price or its own chip kit's Edu numbers are not stored."""
    laptop = session.get(Laptop, laptop_id)
    if laptop is None or laptop.status != LaptopStatus.ACTIVE.value or not laptop.part_numbers:
        raise NotConfigurable
    brand = session.get(LaptopBrand, laptop.brand_id)
    match = _CODE.match(laptop.model_code or "")
    if brand is None or brand.name.lower() != "apple" or match is None:
        raise NotConfigurable
    key = {"product": f"MacBook {match.group(1).title()}", "size_inch": int(match.group(2)),
           "line": line_of_code(laptop.model_code) or ""}
    rows = [r.model_dump() for r in session.exec(
        select(LaptopOption).where(*(getattr(LaptopOption, k) == v for k, v in key.items()))).all()]
    official = _build(laptop, rows, laptop.price_rm)
    if not edu:
        return official
    price = edu_row(session, laptop)
    if price is None:
        raise NoEduPrice
    try:
        return dataclasses.replace(_build(laptop, edu_rows(rows), price.price_rm), edu=True)
    except NotConfigurable:
        raise NoEduPrice from None


def _build(laptop: Laptop, rows: list[dict], price_rm: float) -> Configurable:
    kits = apple_options.kits_from_rows(rows)
    chip = next((c for c in kits if apple_options.cores_of(c) == (laptop.cpu_cores, laptop.gpu_cores)), None)
    if chip is None:
        raise NotConfigurable
    standard = {"memory": _value(laptop.ram_gb), "storage": _value(laptop.ssd_gb)}
    for group in GROUPS[2:]:
        if (value := _included(kits[chip], group)) is not None:
            standard[group] = value
    return Configurable(laptop, rows, kits, chip, standard, price_rm)


def is_configurable(session: Session, laptop_id: uuid.UUID) -> bool:
    try:
        configurable(session, laptop_id)
        return True
    except NotConfigurable:
        return False


def kits_listing(c: Configurable) -> list[dict]:
    """Every chip kit, cheapest first, with its unblocked options per group."""
    kits = []
    for chip, kit in c.kits.items():
        row = chip_row(c, chip)
        options: dict[str, list[dict]] = {}
        for o in kit.options:
            if not o.is_blocked:
                options.setdefault(o.group, []).append({
                    "value": o.value, "label": label(c, chip, o.group, o.value),
                    "add_on_rm": o.add_on_rm, "is_included": o.is_included})
        kits.append({"chip": chip, "label": row["label"], "cpu_model": row["cpu_model"],
                     "gpu_model": row["gpu_model"], "kit_price_rm": kit.price_rm,
                     "chip_add_on_rm": round(kit.price_rm - c.standard_price_rm, 2), "options": options})
    return sorted(kits, key=lambda k: k["kit_price_rm"])


def checked_choices(c: Configurable, chip: Optional[str], given: dict[str, Optional[str]]) -> tuple[str, dict]:
    """The chip (default: the standard config's) and every group's choice. Raises
    NotAllowed for a chip not offered or a given choice the chip does not allow; never
    switches the chip by itself."""
    chip = chip or c.chip
    if chip not in c.kits:
        raise NotAllowed("This chip is not offered on this laptop",
                         [{"group": "chip", "value": chip, "allowed": list(c.kits), "offered_by": []}])
    choices = choices_for(c, chip, given)
    problems = apple_options.not_allowed(c.kits, chip, {g: v for g, v in choices.items() if given.get(g)})
    if problems:
        raise NotAllowed("The chosen chip does not allow this option", problems)
    return chip, choices


def choices_for(c: Configurable, chip: str, given: dict[str, Optional[str]]) -> dict[str, str]:
    """Every group the chip's kit has: the value given, else the standard config's own
    when the chip allows it, else what the chip's kit comes with."""
    kit = c.kits[chip]
    choices = {}
    for group in GROUPS:
        if not any(o.group == group for o in kit.options):
            continue
        if given.get(group):
            choices[group] = given[group]
        elif kit.allows(group, c.standard.get(group, "")):
            choices[group] = c.standard[group]
        else:
            choices[group] = _included(kit, group)
    return choices


def total_of(c: Configurable, chip: str, choices: dict[str, str]) -> float:
    """The chip kit's price plus the chosen options' add-ons on that kit."""
    kit = c.kits[chip]
    return round(kit.price_rm + sum(kit.add_on(g, v) for g, v in choices.items()), 2)


def config_path(c: Configurable, chip: str, choices: dict[str, str]) -> str:
    """The details page's path for this configuration (plan step 10d+, 2026-11-12): the
    same names and values /configure takes, in one fixed order so a configuration has
    one URL. Picks that are the standard config's own are the plain path: it is the
    laptop row's own chip, RAM and storage, so its meaning never changes. Otherwise
    chip, memory and storage are always written; adapter and display only when they
    differ from what the chip's kit comes with. The keyboard is never written. The
    frontend's buildConfigPath follows the same rule and order, and both are held to
    tests/unit/fixtures/config_url_cases.json."""
    plain = f"/laptops/{c.laptop.id}"
    if chip == c.chip and all(choices.get(g) == c.standard.get(g) for g in _URL_GROUPS):
        return plain
    kit = c.kits[chip]
    params = [("chip", chip), ("memory", choices["memory"]), ("storage", choices["storage"])]
    params += [(g, choices[g]) for g in ("adapter", "display")
               if g in choices and choices[g] != _included(kit, g)]
    return f"{plain}?{urlencode(params)}"


def chip_row(c: Configurable, chip: str) -> dict:
    return next(r for r in c.rows if r["option_group"] == "chip" and r["value"] == chip)


def label(c: Configurable, chip: str, group: str, value: str) -> Optional[str]:
    return next((r["label"] for r in c.rows if r["chip"] == chip and r["option_group"] == group
                 and r["value"] == value), None)


def _configured_product(laptop: Laptop, chip: dict, choices: dict[str, str], total_rm: float):
    return dataclasses.replace(
        laptop_to_scorable(laptop, "Apple"), price=total_rm, ram_gb=_gb(choices["memory"]),
        storage_gb=_gb(choices["storage"]), cpu_model=chip["cpu_model"], gpu_model=chip["gpu_model"])


def live_scores(laptop: Laptop, chip: dict, choices: dict[str, str], total_rm: float,
                scoring: tuple) -> tuple[Optional[list[dict]], Optional[str]]:
    """The detail page's five use-case scores for the configured machine, or None and why."""
    return _use_case_scores(_configured_product(laptop, chip, choices, total_rm), scoring)


def _use_case_scores(product, scoring: tuple) -> tuple[Optional[list[dict]], Optional[str]]:
    ranges, cpu_bm, gpu_bm = scoring
    results = {use_case: calculate_pick_score(product, None, ranges, cpu_bm, gpu_bm, priority_override=priorities)
               for use_case, priorities in USE_CASE_PRIORITIES.items()}
    # The engine's own rule (ADR-0016), as on the detail page: one unresolved part scores a
    # flagged neutral 50; both unresolved withholds the score.
    if any(r.score is None for r in results.values()):
        return None, (f"No benchmark for this chip's CPU or GPU ({product.cpu_model}, "
                      f"{product.gpu_model}), so there is no Pick Score rather than a made-up one.")
    return [{"use_case": use_case, "score": r.score, "breakdown": [b.model_dump() for b in r.breakdown],
             "flags": r.flags} for use_case, r in results.items()], None


def personal_score(laptop: Laptop, chip: dict, choices: dict[str, str], total_rm: float,
                   scoring: tuple, user_pref) -> dict:
    """The caller's personalized score for the configured machine: the engine's
    personalized mode, as search_laptops runs it for a user with a preference row."""
    return _personal(_configured_product(laptop, chip, choices, total_rm), scoring, user_pref)


def _personal(product, scoring: tuple, user_pref) -> dict:
    ranges, cpu_bm, gpu_bm = scoring
    r = calculate_pick_score(product, user_pref, ranges, cpu_bm, gpu_bm,
                             priority_override=USE_CASE_PRIORITIES["general_use"])
    return {"score": r.score, "mode": r.mode, "breakdown": [b.model_dump() for b in r.breakdown], "flags": r.flags}


def edu_view(session: Session, laptop: Laptop, user_pref=None) -> Optional[dict[str, Any]]:
    """The laptop at its Edu price, for the details page's toggle: the price, when it was
    checked, and the Pick Scores calculated live from it (a personalized one's budget
    comparison included: it is the price this user would pay). None when it has no Edu
    price to show. The ranges are the catalog's official prices, unchanged."""
    row = edu_row(session, laptop)
    if row is None:
        return None
    brand = session.get(LaptopBrand, laptop.brand_id)
    product = dataclasses.replace(laptop_to_scorable(laptop, brand.name if brand else ""), price=row.price_rm)
    scoring = get_scoring_data(session)
    scores, reason = _use_case_scores(product, scoring)
    return {
        "price_rm": row.price_rm, "official_price_rm": laptop.price_rm,
        "saving_rm": round(laptop.price_rm - row.price_rm, 2),
        "checked_at": row.checked_at, "source": row.source, "source_url": row.source_url,
        "scores": scores, "score_unavailable_reason": reason,
        "personalized_score": _personal(product, scoring, user_pref) if user_pref is not None else None,
    }


def configure(session: Session, c: Configurable, chip: str, choices: dict[str, str],
              user_pref=None) -> dict[str, Any]:
    """user_pref: the caller's LaptopUserPreference, or None (no personalized score)."""
    kit = c.kits[chip]
    add_ons = [{"group": g, "value": v, "label": label(c, chip, g, v), "add_on_rm": kit.add_on(g, v)}
               for g, v in choices.items()]
    total = total_of(c, chip, choices)
    row = chip_row(c, chip)
    scoring = get_scoring_data(session)
    scores, reason = live_scores(c.laptop, row, choices, total, scoring)
    return {
        "laptop_id": c.laptop.id, "chip": chip, "chip_label": row["label"],
        "cpu_model": row["cpu_model"], "gpu_model": row["gpu_model"],
        "standard_price_rm": c.standard_price_rm, "chip_add_on_rm": round(kit.price_rm - c.standard_price_rm, 2),
        "edu": c.edu, "add_ons": add_ons, "total_rm": total, "scores": scores, "score_unavailable_reason": reason,
        "personalized_score": (personal_score(c.laptop, row, choices, total, scoring, user_pref)
                               if user_pref is not None else None),
    }
