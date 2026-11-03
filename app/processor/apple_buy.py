"""
What processing an Apple buy page will do, decided before any LLM call (2026-11-03).

The buy page says which configurations exist and what they cost; the specs page says
what they are. Code builds each configuration's identity (model_code, name), matches it
to a stored row and decides its price. The LLM is only asked for the shared specs per
size and chip, and is never asked which configurations exist.

Pure: no database, no network. app/processor/engine.py feeds it and applies the plan,
and the read-only prediction uses the same plan, so a prediction and a run cannot
disagree.
"""
from typing import Any, Iterable, Mapping, Optional

from app.processor.apple_name import build_code, identity, line_of_code, only_value

# A price with one of these latest sources may be replaced by the buy page's. A guide,
# manual or website price is someone's decision and is listed instead.
REPLACEABLE_SOURCES = {None, "apple-buy-page"}

# \s also matches the no-break spaces Apple's pages use ("8GB unified\xa0memory").
_CHIP = r"Apple\s+([AM]\d+(?:\s+(?:Pro|Max|Ultra))?)\s+chip"
_RAM = r"(\d+)GB\s+unified\s+memory"
_SIZE = r"(\d+)(?:\.\d)?-inch"


def resolve(configs: Iterable[Mapping], sections: list[str]) -> tuple[list[dict], list[dict]]:
    """Configurations with every identity field set, and those that cannot be. A field
    the buy page does not state (Neo's links name only storage) is read from the specs
    page, and only when the page states exactly one value for it."""
    text = "\n".join(sections)
    page = {"chip": only_value(_CHIP, text), "ram_gb": only_value(_RAM, text), "size": only_value(_SIZE, text)}
    resolved, unresolved = [], []
    for config in configs:
        config = dict(config)
        for field, value in page.items():
            if config.get(field) is None and value is not None:
                config[field] = " ".join(value.split()) if field == "chip" else int(value)
        missing = [f for f in ("size", "chip", "cpu_cores", "gpu_cores", "ram_gb", "ssd_gb") if config.get(f) is None]
        (unresolved if missing else resolved).append(
            {**config, "missing": missing} if missing else config
        )
    return resolved, unresolved


def code_of(config: Mapping, product: str, line: Optional[str]) -> str:
    return build_code(product, line, config["size"], config["chip"], config["cpu_cores"],
                      config["gpu_cores"], config["ram_gb"], config["ssd_gb"])


def template_key(size, chip, cpu, gpu) -> tuple:
    """Which extracted variant a configuration takes its shared specs from."""
    return (int(size) if size else None, chip, cpu, gpu)


def _same_page(row: Mapping, product: str, line: Optional[str]) -> bool:
    code = row["model_code"]
    return code.startswith(f"apple-{product.lower().replace(' ', '-')}-") and line_of_code(code) == line


def plan(
    configs: Iterable[Mapping],
    product: str,
    line: Optional[str],
    rows: Iterable[Mapping],
    latest_source: Mapping[Any, Optional[str]],
) -> dict:
    """
    rows: every Apple laptop (id, model_code, status, part_numbers, price_rm,
    display_size_inch, processor_model). latest_source: laptop id -> source of its newest
    price-history row (absent = no history).
    """
    rows = [dict(r) for r in rows]
    by_part = {p: r for r in rows for p in (r.get("part_numbers") or [])}
    by_code = {r["model_code"]: r for r in rows}

    result: dict[str, list] = {
        "inserts": [], "updates": [], "matched_suspended": [], "identity_conflicts": [],
        "price_changes": [], "prices_not_overwritten": [], "not_on_buy_page": [],
    }
    matched = set()
    for config in configs:
        code = code_of(config, product, line)
        row = next((by_part[p] for p in config.get("part_numbers") or [] if p in by_part), None) or by_code.get(code)
        if row is None:
            result["inserts"].append({"config": dict(config), "model_code": code})
            continue
        matched.add(row["id"])
        if row["status"] == "suspended":
            result["matched_suspended"].append({"laptop_id": str(row["id"]), "model_code": row["model_code"]})
            continue
        was = identity(row["display_size_inch"], row["processor_model"], row["model_code"])
        now = {"screen_inch": config["size"], "chip": config["chip"], "line": line}
        if was != now:
            result["identity_conflicts"].append({
                "laptop_id": str(row["id"]), "model_code": row["model_code"], "existing": was, "incoming": now,
            })
            continue
        price = config.get("price_rm")
        source = latest_source.get(row["id"])
        write_price = bool(price) and price != row["price_rm"] and (not row["price_rm"] or source in REPLACEABLE_SOURCES)
        if price and price != row["price_rm"]:
            entry = {"laptop_id": str(row["id"]), "model_code": row["model_code"], "status": row["status"],
                     "old": row["price_rm"], "new": price}
            result["price_changes" if write_price else "prices_not_overwritten"].append(
                entry if write_price else {**entry, "source": source}
            )
        result["updates"].append({
            "laptop_id": str(row["id"]), "model_code": row["model_code"], "status": row["status"],
            "config": dict(config), "write_price": write_price,
        })

    result["not_on_buy_page"] = [
        {"laptop_id": str(r["id"]), "model_code": r["model_code"], "status": r["status"]}
        for r in rows if r["id"] not in matched and _same_page(r, product, line)
    ]
    return result
