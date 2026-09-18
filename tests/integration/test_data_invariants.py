"""
Catalog invariants, expressed as tests instead of constraints.

A note on what these can and cannot do. Run against the throwaway container they
check the QUERY, not the catalog: the container starts empty, so an invariant
over "every active laptop" is vacuously true until this file seeds a violation.
Each test therefore seeds one, asserts the query finds it, and asserts a clean
catalog comes back clean — testing the checker, which is the part that can rot.

The invariants are not constraints because two of the three cannot be: a CHECK
on price_rm > 0 would reject the AI processor's legitimate intermediate state
(scraped, unpriced, status inactive — ADR-0009's work queue), and gpu_mark is
not a column at all, it is the output of a three-path resolution.
"""

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_models import Laptop
from app.pickscore.benchmark_service import resolve_gpu_benchmark
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration



def _unpriced_active(session) -> list[Laptop]:
    return list(
        session.exec(
            select(Laptop).where(Laptop.status == "active").where(Laptop.price_rm <= 0)
        ).all()
    )


def test_an_active_laptop_with_no_price_is_a_violation(session, brand):
    """
    price_rm = 0 means "unknown", not "free", and both the budget filter
    (`price_rm <= budget_max`) and the reranker's budget penalty treat it as
    trivially affordable — which is how nine of the twenty most-recommended
    laptops came to be unpriced rows (ADR-0009). An unpriced row belongs in
    `inactive`, the work queue, not in `active`.

    The ORM will no longer produce this state -- `_deactivate_unpriced` demotes
    an unpriced row on every insert and update -- so the violation is forced
    through a Core UPDATE, which fires no mapper event. That is not cheating:
    the row is still reachable by hand, by a migration, or by a path predating
    the rule, which is exactly what leaves a checker something to find.
    """
    unpriced = make_laptop(brand.id, price_rm=0.0)
    session.add(unpriced)
    session.commit()
    session.execute(
        update(Laptop).where(Laptop.id == unpriced.id).values(status="active")
    )
    session.commit()
    assert len(_unpriced_active(session)) == 1


def test_an_unpriced_inactive_laptop_is_not_a_violation(session, brand):
    """The negative half, and the reason this is not a CHECK constraint: an
    unpriced row is a legitimate state as long as it is not recommendable."""
    session.add(make_laptop(brand.id, price_rm=0.0, status="inactive"))
    session.commit()
    assert _unpriced_active(session) == []


def test_a_priced_catalog_is_clean(session, brand):
    session.add(make_laptop(brand.id, price_rm=4000.0, status="active"))
    session.commit()
    assert _unpriced_active(session) == []


# --------------------------------------------------------------------------
# Every active laptop resolves to a gpu_mark, by one of the three paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gpu_model,cpu_model,expected_path",
    [
        ("NVIDIA GeForce RTX 5060 Laptop GPU", "Intel Core i7-14650HX", "discrete"),
        ("Intel Arc Graphics", "Intel Core Ultra 7 255H", "integrated"),
        ("40-core GPU", "Apple M5 Max", "apple"),  # the catalog's own form
    ],
)
def test_each_gpu_path_resolves(gpu_model, cpu_model, expected_path, gpu_table):
    """
    Three paths, no unhandled case. The one that matters is `integrated`: a GPU
    string with no model-number token identifies no specific part, so a fuzzy
    match lands on whatever looks nearest — which is how "AMD Radeon Graphics"
    matched a pre-2005 row at 0.855 confidence, above any threshold that could
    have separated it.
    """
    result = resolve_gpu_benchmark(gpu_model, cpu_model, gpu_table)
    assert result["score"] is not None, f"{expected_path} path did not resolve"


def test_an_unresolvable_gpu_is_flagged_rather_than_scored(gpu_table):
    """The fourth outcome is allowed to exist, but must be marked: a neutral 50
    outranks a real RTX 3050 on the current gpu_mark range, so an unknown GPU
    that is not flagged can beat a measured one."""
    result = resolve_gpu_benchmark("Fictional GPU", "Fictional CPU", gpu_table)
    assert result["score"] is None
    assert result["is_proxy"] is True


@pytest.fixture
def gpu_table():
    return [
        ("GeForce RTX 5060 Laptop GPU", 16785),
        ("Intel Arc 140T GPU", 6607),
        ("GeForce RTX 5070 Ti Laptop GPU", 22465),
        ("Intel UHD Graphics", 1533),
    ]


# --------------------------------------------------------------------------
# laptop_family has no orphan family_id
# --------------------------------------------------------------------------


def _orphan_family_ids(session) -> list:
    family_ids = {f.id for f in session.exec(select(LaptopFamily)).all()}
    return [
        laptop.id
        for laptop in session.exec(select(Laptop)).all()
        if laptop.family_id is not None and laptop.family_id not in family_ids
    ]


def test_a_null_family_id_is_not_an_orphan(session, brand):
    """Null means "not grouped yet" and is a valid state — it is the
    /families/regroup backlog, not a defect. An invariant that flagged it would
    be red permanently and get ignored."""
    session.add(make_laptop(brand.id, family_id=None))
    session.commit()
    assert _orphan_family_ids(session) == []


def test_a_family_id_pointing_nowhere_is_an_orphan(session, brand):
    """The FK makes this unreachable through the database, so what this really
    guards is a future code path that writes family_id without a row — the
    processor's upsert and the bulk move both touch it."""
    family = LaptopFamily(brand_id=brand.id, name="TUF Gaming F16", family_key="tuf gaming f16")
    session.add(family)
    session.commit()
    session.refresh(family)

    laptop = make_laptop(brand.id, family_id=family.id)
    session.add(laptop)
    session.commit()
    assert _orphan_family_ids(session) == []
