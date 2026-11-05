"""
get_configuration_options: Pico's Apple configurator (step 10c, step (e)).

The same logic as GET /laptops/{id}/options and /configure (app/laptops/options_service):
without selections, the laptop's chip kits and each kit's add-ons; with selections, the
final configuration, its total and its live Pick Score. A choice the chip does not allow
is returned with the chips that offer it, never switched to silently.

Compact on purpose: the eval keeps 4,500 characters per tool output, so add-ons go out
as value -> RM and the keyboard group (no prices) is left out of the listing.
"""
import re
import uuid
from typing import Optional

from langchain_core.tools import tool
from sqlmodel import Session

from app.database import engine
from app.laptops import options_service as svc

_LISTED = ("memory", "storage", "adapter", "display")
_ALIASES = {"ram": "memory", "ssd": "storage", "power_adapter": "adapter", "chip": "chip"}
_SPACE = re.compile(r"[\s_-]+")

_LISTING_NOTE = (
    "Each chip's add-ons are RM relative to that chip's price_rm; a chip's add_on_rm is "
    "relative to this standard config's price. A configured machine is not a catalog "
    "laptop. For an exact total and its live Pick Score, call again with selections."
)
_CONFIGURED_NOTE = (
    "A configured machine, not a catalog laptop. Its Pick Scores are live, scored against "
    "the catalog's standard configs; it has no catalog ranking."
)


def _value(group: str, raw: str) -> str:
    v = raw.strip().lower()
    if group in ("memory", "storage", "adapter"):
        return v.replace(" ", "")          # "48 GB" -> "48gb"
    if group == "display":
        return _SPACE.sub("_", v).replace("_glass", "")  # "Nano-texture glass" -> "nano_texture"
    return v


def _listing(c: svc.Configurable) -> dict:
    chips = []
    for k in svc.kits_listing(c):
        chip = {"chip": k["chip"], "label": k["label"], "cpu": k["cpu_model"], "gpu": k["gpu_model"],
                "price_rm": k["kit_price_rm"], "add_on_rm": k["chip_add_on_rm"]}
        for group in _LISTED:
            if k["options"].get(group):
                chip[group] = {o["value"]: o["add_on_rm"] or 0.0 for o in k["options"][group]}
        chips.append(chip)
    return {"laptop_id": str(c.laptop.id), "product_name": c.laptop.product_name,
            "standard_price_rm": c.laptop.price_rm,
            "standard": {"chip": c.chip, **{g: v for g, v in c.standard.items() if g in _LISTED}},
            "chips": chips, "note": _LISTING_NOTE}


def _labelled(c: svc.Configurable, problems: list[dict]) -> list[dict]:
    """not_allowed with each offering chip's label and price, so Pico can name it."""
    out = []
    for p in problems:
        offered = [{"chip": chip, "label": svc.chip_row(c, chip)["label"], "price_rm": c.kits[chip].price_rm}
                   for chip in p["offered_by"]]
        out.append({"group": p["group"], "value": p["value"], "allowed_on_this_chip": p["allowed"],
                    "offered_by": offered})
    return out


def _configured(c: svc.Configurable, result: dict) -> dict:
    out = {"laptop_id": str(c.laptop.id), "product_name": c.laptop.product_name, "configured": True,
           "chip": result["chip"], "chip_label": result["chip_label"],
           "cpu": result["cpu_model"], "gpu": result["gpu_model"],
           "standard_price_rm": result["standard_price_rm"], "chip_add_on_rm": result["chip_add_on_rm"],
           "add_ons": {a["group"]: {"value": a["value"], "add_on_rm": a["add_on_rm"]}
                       for a in result["add_ons"] if a["group"] in _LISTED},
           "total_rm": result["total_rm"]}
    if result["scores"] is None:
        out["pick_score"] = None
        out["pick_score_unavailable"] = result["score_unavailable_reason"]
    else:
        out["live_pick_scores"] = {s["use_case"]: s["score"] for s in result["scores"]}
    out["note"] = _CONFIGURED_NOTE
    return out


def configuration_options(session: Session, laptop_id: str, selections: Optional[dict] = None) -> dict:
    try:
        target = uuid.UUID(laptop_id)
    except ValueError:
        return {"error": f"'{laptop_id}' is not a laptop id; pass the laptop_id from search_laptops or lookup_laptop."}
    try:
        c = svc.configurable(session, target)
    except svc.NotConfigurable:
        return {"error": "This laptop has no build-to-order options. Only active Apple MacBook standard "
                         "configurations have them; do not quote upgrade prices for it."}
    if not selections:
        return _listing(c)
    given: dict[str, str] = {}
    for key, raw in selections.items():
        group = _ALIASES.get(key.strip().lower(), key.strip().lower())
        if group not in ("chip", *svc.GROUPS):
            return {"error": f"Unknown selection '{key}'. Use chip, memory, storage, adapter or display."}
        if raw:
            given[group] = _value(group, str(raw))
    chip = given.pop("chip", None)
    try:
        chip, choices = svc.checked_choices(c, chip, given)
    except svc.NotAllowed as e:
        return {"laptop_id": str(c.laptop.id), "product_name": c.laptop.product_name,
                "error": e.message, "chip": chip or c.chip, "not_allowed": _labelled(c, e.problems),
                "instruction": ("Tell the user which chip offers the choice and what that chip costs; "
                                "do not switch the chip for them. To price it, call again with that chip.")}
    return _configured(c, svc.configure(session, c, chip, choices))


@tool
def get_configuration_options(laptop_id: str, selections: Optional[dict[str, str]] = None) -> dict:
    """Build-to-order options of an Apple MacBook standard configuration, from Apple's
    own configurator prices.

    Without selections: its chips (each with price_rm and add_on_rm vs this config)
    and, per chip, the memory/storage/adapter/display values with their add-on in RM.
    With selections, e.g. {"memory": "48gb", "storage": "2tb"} or
    {"chip": "m5pro-18-20", "memory": "64gb"}: the configured total and its live Pick
    Scores. chip defaults to this configuration's own; use a chip value from the
    listing. A choice this chip does not allow comes back as not_allowed with the
    chips that offer it: tell the user which chip, never pick it for them.

    Args:
        laptop_id: UUID of a MacBook standard config (from search_laptops or lookup_laptop).
        selections: Optional group -> value: chip, memory, storage, adapter, display.
    """
    with Session(engine) as session:
        return configuration_options(session, laptop_id, selections)
