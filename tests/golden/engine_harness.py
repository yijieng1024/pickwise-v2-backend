"""
Run the real scoring engine over the frozen fixture.

The two things this file exists to get right:

1. THE RANGES COME FROM PRODUCTION, not from a copy of its arithmetic here.
   `get_laptop_ranges` is what decides which rows enter each denominator --
   including the `status == active` filter that keeps a retired laptop out --
   and a reimplementation would drift from it silently, which is the one
   failure a golden snapshot cannot survive. So the fixture is loaded into an
   in-memory SQLite database and the production function is called against it.
   No Postgres, no container, no network: `laptops` needs only the seven
   columns that query selects plus `status`, and the two benchmark tables are
   plain enough for SQLModel to create anywhere.

2. THE BENCHMARKS ARE FROZEN. The engine resolves marks against whatever rows
   it is handed, so handing it the 2852-row production table would mean a
   PassMark re-scrape moves the golden file and every future diff mixes "the
   engine changed" with "NVIDIA released something". Only the rows these 20
   laptops resolve to are stored, and they are stored by name so the resolution
   path itself still runs.

No production code was changed to make this possible: `calculate_pick_score`
already takes both benchmark lists as arguments, and `get_laptop_ranges`
already takes a session.
"""

import json
import pathlib
import uuid

from sqlalchemy import text
from sqlmodel import Session, create_engine

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.pickscore_adapter import get_laptop_ranges
from app.laptops.pickscore_general import USE_CASE_PRIORITIES
from app.pickscore import benchmark_service as _bench
from app.pickscore.benchmark_service import resolve_benchmark, resolve_gpu_benchmark
from app.pickscore.engine import calculate_pick_score
from app.pickscore.schemas import ScorableProduct

FIXTURE_PATH = pathlib.Path(__file__).parent / "pickscore_fixture.json"
GOLDEN_PATH = pathlib.Path(__file__).parent / "pickscore.json"

# The seven columns get_laptop_ranges selects, plus the status it filters on.
# Deliberately not the full 200-field model: the production query names these
# and nothing else, and a JSONB or VECTOR column cannot be created on SQLite.
_LAPTOPS_DDL = """
CREATE TABLE laptops (
    status TEXT,
    price_rm REAL,
    ram_gb INTEGER,
    ssd_gb INTEGER,
    weight_kg REAL,
    battery_wh REAL,
    processor_model TEXT,
    gpu_model TEXT
)
"""


def clear_benchmark_cache() -> None:
    """
    benchmark_service._cache is module-level and keyed ONLY on the normalized
    model string -- not on which benchmark table was passed. So a resolution
    another test did against its own small table is served straight back to us
    for the same string, and the frozen marks stop being frozen.

    That is the open cache-key xfail in
    tests/integration/test_benchmark_resolution.py, observed here rather than
    argued: without this call the golden snapshot fails when it runs after
    tests/integration/test_data_invariants.py, and passes when it runs alone.
    Clearing is the test-side workaround; a table discriminator in the key is
    the fix.
    """
    _bench._cache.clear()


def load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def frozen_benchmarks(fixture: dict):
    """(name, mark) tuples, the shape the engine takes."""
    return (
        [tuple(row) for row in fixture["cpu_benchmarks"]],
        [tuple(row) for row in fixture["gpu_benchmarks"]],
    )


def fixture_ranges(fixture: dict) -> dict:
    """Production's get_laptop_ranges, over the fixture, in memory."""
    clear_benchmark_cache()
    engine = create_engine("sqlite://")
    CPUBenchmark.__table__.create(engine)
    GPUBenchmark.__table__.create(engine)
    with engine.connect() as conn:
        conn.execute(text(_LAPTOPS_DDL))
        for row in fixture["laptops"]:
            conn.execute(
                text(
                    "INSERT INTO laptops VALUES (:status, :price_rm, :ram_gb, :ssd_gb,"
                    " :weight_kg, :battery_wh, :processor_model, :gpu_model)"
                ),
                {k: row[k] for k in (
                    "status", "price_rm", "ram_gb", "ssd_gb", "weight_kg",
                    "battery_wh", "processor_model", "gpu_model",
                )},
            )
        conn.commit()

    with Session(engine) as session:
        for name, mark in fixture["cpu_benchmarks"]:
            session.add(CPUBenchmark(cpu_name=name, cpu_mark=mark))
        for name, mark in fixture["gpu_benchmarks"]:
            session.add(GPUBenchmark(gpu_name=name, gpu_mark=mark))
        session.commit()
        # force_refresh: the ranges cache is module-level and would otherwise
        # hand back whatever the last caller computed, including a mutated run.
        return get_laptop_ranges(session, force_refresh=True)


def _scorable(row: dict) -> ScorableProduct:
    return ScorableProduct(
        # Deterministic id from the key: a random uuid4 would change the
        # snapshot on every run.
        product_id=uuid.uuid5(uuid.NAMESPACE_OID, row["key"]),
        brand_name=row["brand"],
        price=row["price_rm"],
        cpu_model=row["processor_model"],
        gpu_model=row["gpu_model"],
        ram_gb=row["ram_gb"],
        storage_gb=row["ssd_gb"],
        storage_type=row["storage_type"],
        weight_kg=row["weight_kg"],
        battery_wh=row["battery_wh"],
        display_size_inch=row["display_size_inch"],
    )


def compute_snapshot(fixture: dict) -> dict:
    """Every laptop x every use case, plus the marks that produced them."""
    clear_benchmark_cache()
    cpu_bm, gpu_bm = frozen_benchmarks(fixture)
    ranges = fixture_ranges(fixture)
    clear_benchmark_cache()

    snapshot: dict = {}
    for row in fixture["laptops"]:
        product = _scorable(row)

        # Recorded because _score_gpu has no flags entry for an unresolved
        # benchmark, so a rejected match and a genuine mid score are the same
        # number in the breakdown (the open xfail in
        # tests/unit/test_pickscore_factors.py). Storing the mark itself means
        # a diff shows `gpu_mark: 6607 -> null` even though no flag says so.
        # A WORKAROUND, not a fix -- the flag is still the real answer.
        cpu_mark = resolve_benchmark(product.cpu_model, cpu_bm)["score"]
        gpu_mark = resolve_gpu_benchmark(product.gpu_model, product.cpu_model, gpu_bm)["score"]

        per_use_case = {}
        for use_case, profile in sorted(USE_CASE_PRIORITIES.items()):
            result = calculate_pick_score(
                product=product,
                user_pref=None,           # general mode: the snapshot must not
                ranges=ranges,            # depend on a user row
                cpu_benchmarks=cpu_bm,
                gpu_benchmarks=gpu_bm,
                priority_override=profile,
            )
            per_use_case[use_case] = {
                "score": result.score,
                "flags": result.flags,
                "factors": {
                    b.factor: {
                        "raw_score": b.raw_score,
                        "weight": b.weight,
                        "contribution": b.contribution,
                        "note": b.note,
                    }
                    for b in result.breakdown
                },
            }

        snapshot[row["key"]] = {
            "product_name": row["product_name"],
            "status": row["status"],
            "cpu_mark": cpu_mark,
            "gpu_mark": gpu_mark,
            "use_cases": per_use_case,
        }
    return snapshot


def flatten(snapshot: dict) -> dict:
    """One flat dict of path -> value, so a diff can name what moved."""
    flat: dict[tuple, object] = {}
    for key, entry in snapshot.items():
        flat[(key, "-", "cpu_mark")] = entry["cpu_mark"]
        flat[(key, "-", "gpu_mark")] = entry["gpu_mark"]
        for use_case, data in entry["use_cases"].items():
            flat[(key, use_case, "score")] = data["score"]
            for flag, value in data["flags"].items():
                flat[(key, use_case, f"flag.{flag}")] = value
            for factor, parts in data["factors"].items():
                flat[(key, use_case, f"{factor}.raw")] = parts["raw_score"]
                flat[(key, use_case, f"{factor}.weight")] = parts["weight"]
    return flat
