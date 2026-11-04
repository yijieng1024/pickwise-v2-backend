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
"""
import dataclasses
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlmodel import Session, select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.laptops.option_model import LaptopOption
from app.laptops.pickscore_adapter import get_scoring_data, laptop_to_scorable
from app.laptops.pickscore_general import USE_CASE_PRIORITIES
from app.pickscore.engine import calculate_pick_score
from app.processor import apple_options
from app.processor.apple_name import line_of_code

GROUPS = ("memory", "storage", "adapter", "display", "keyboard")
_CODE = re.compile(r"^apple-macbook-(air|pro)-(\d+)-")
_SIZE = re.compile(r"(\d+)(gb|tb)")


class NotConfigurable(LookupError):
    """Not an active Apple standard config with stored chip kits."""


@dataclass
class Configurable:
    laptop: Laptop
    rows: list[dict]
    kits: dict[str, apple_options.Kit]
    chip: str                       # the standard config's own chip
    standard: dict[str, str]        # group -> the standard config's own value


def _value(gb: int) -> str:
    return f"{gb // 1024}tb" if gb >= 1024 and gb % 1024 == 0 else f"{gb}gb"


def _gb(value: str) -> int:
    match = _SIZE.fullmatch(value)
    return int(match.group(1)) * (1024 if match.group(2) == "tb" else 1)


def _included(kit: apple_options.Kit, group: str) -> Optional[str]:
    return next((o.value for o in kit.options if o.group == group and o.is_included), None)


def configurable(session: Session, laptop_id: uuid.UUID) -> Configurable:
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
    kits = apple_options.kits_from_rows(rows)
    chip = next((c for c in kits if apple_options.cores_of(c) == (laptop.cpu_cores, laptop.gpu_cores)), None)
    if chip is None:
        raise NotConfigurable
    standard = {"memory": _value(laptop.ram_gb), "storage": _value(laptop.ssd_gb)}
    for group in GROUPS[2:]:
        if (value := _included(kits[chip], group)) is not None:
            standard[group] = value
    return Configurable(laptop, rows, kits, chip, standard)


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


def chip_row(c: Configurable, chip: str) -> dict:
    return next(r for r in c.rows if r["option_group"] == "chip" and r["value"] == chip)


def label(c: Configurable, chip: str, group: str, value: str) -> Optional[str]:
    return next((r["label"] for r in c.rows if r["chip"] == chip and r["option_group"] == group
                 and r["value"] == value), None)


def live_scores(laptop: Laptop, chip: dict, choices: dict[str, str], total_rm: float,
                scoring: tuple) -> tuple[Optional[list[dict]], Optional[str]]:
    """The detail page's five use-case scores for the configured machine, or None and why."""
    ranges, cpu_bm, gpu_bm = scoring
    product = dataclasses.replace(
        laptop_to_scorable(laptop, "Apple"), price=total_rm, ram_gb=_gb(choices["memory"]),
        storage_gb=_gb(choices["storage"]), cpu_model=chip["cpu_model"], gpu_model=chip["gpu_model"])
    results = {use_case: calculate_pick_score(product, None, ranges, cpu_bm, gpu_bm, priority_override=priorities)
               for use_case, priorities in USE_CASE_PRIORITIES.items()}
    # The engine's own rule (ADR-0016), as on the detail page: one unresolved part scores a
    # flagged neutral 50; both unresolved withholds the score.
    if any(r.score is None for r in results.values()):
        return None, (f"No benchmark for this chip's CPU or GPU ({chip['cpu_model']}, "
                      f"{chip['gpu_model']}), so there is no Pick Score rather than a made-up one.")
    return [{"use_case": use_case, "score": r.score, "breakdown": [b.model_dump() for b in r.breakdown],
             "flags": r.flags} for use_case, r in results.items()], None


def configure(session: Session, c: Configurable, chip: str, choices: dict[str, str]) -> dict[str, Any]:
    kit = c.kits[chip]
    add_ons = [{"group": g, "value": v, "label": label(c, chip, g, v), "add_on_rm": kit.add_on(g, v)}
               for g, v in choices.items()]
    total = round(kit.price_rm + sum(a["add_on_rm"] for a in add_ons), 2)
    row = chip_row(c, chip)
    scores, reason = live_scores(c.laptop, row, choices, total, get_scoring_data(session))
    return {
        "laptop_id": c.laptop.id, "chip": chip, "chip_label": row["label"],
        "cpu_model": row["cpu_model"], "gpu_model": row["gpu_model"],
        "standard_price_rm": c.laptop.price_rm, "chip_add_on_rm": round(kit.price_rm - c.laptop.price_rm, 2),
        "add_ons": add_ons, "total_rm": total, "scores": scores, "score_unavailable_reason": reason,
    }
