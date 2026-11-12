"""
Education prices (step 10d) against the test database: the two reads behind the details
page's Edu toggle, the education-store pass of the Apple options fetch, and the second
half of the guard: Pico's retrieval, the ranking, the price filter, the scoring ranges
and generate-all return the same with and without an Edu price stored.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops import laptop_router
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopEduPrice, LaptopPickScore
from app.laptops.option_model import LaptopOption
from app.laptops.pickscore_adapter import get_laptop_ranges
from app.laptops.pickscore_general import generate_all_pick_scores, get_ranking_for_use_case
from app.rag import retrieval
from app.scraper import apple_options_fetch as fetch
from app.scraper.models import RawScrapLaptop
from app.users.models import LaptopUserPreference
from tests._apple_fake import AIR13_1010_SLUG, AIR13_SLUG, AIR_BUY, AIR_SPECS, FakeApple, FakeAppleEdu, fixture
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

KITS = Path(__file__).parents[1] / "unit" / "fixtures" / "apple_kits_pro_2026-11-04.json"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
UNFETCHED = "m5max-18-40"   # one kit the Edu pass never reached


def _apple(session) -> LaptopBrand:
    apple = LaptopBrand(name="Apple", base_scrape_url="https://www.apple.com/my/mac/")
    session.add(apple)
    session.flush()
    return apple


@pytest.fixture
def m6(session):
    """The Pro 14 M6 standard config (RM 8,299) and its line's chip kits, not yet on the
    education store."""
    apple = _apple(session)
    for row in json.loads(KITS.read_text(encoding="utf-8")):
        if row["size_inch"] == 14 and row["line"] == "liquid-retina":
            session.add(LaptopOption(**row))
    laptop = make_laptop(apple.id, model_code="apple-macbook-pro-14-liquid-retina-m6-12c-12g-16gb-1024gb",
                         product_name="MacBook Pro 14 (M6)", price_rm=8299.0, processor_model="Apple M6 (12-core)",
                         gpu_model="12-core GPU", cpu_cores=12, gpu_cores=12, ram_gb=16, ssd_gb=1024,
                         display_size_inch=14.2, part_numbers=["MHWT4ZP/A"])
    session.add(laptop)
    session.add(make_laptop(apple.id, product_name="Another laptop", price_rm=5000.0))
    # The M6's own benchmarks (ADR-0010 Amendment III), so its scores are numbers.
    session.add(CPUBenchmark(cpu_name="ARM Apple M6 12 Core 0 MHz", cpu_mark=35823))
    session.add(GPUBenchmark(gpu_name="Intel Arc B390 GPU", gpu_mark=9000))
    session.commit()
    return laptop


def _store_edu(session, laptop, price_rm=7899.0):
    """As after an Edu pass: every add-on 10% lower, every kit RM 400 lower, one kit not reached."""
    for option in session.exec(select(LaptopOption).where(LaptopOption.chip != UNFETCHED)):
        option.edu_fetched_at = NOW
        option.edu_add_on_rm = None if option.add_on_rm is None else round(option.add_on_rm * 0.9, 2)
        option.edu_kit_price_rm = None if option.kit_price_rm is None else option.kit_price_rm - 400
        session.add(option)
    session.add(LaptopEduPrice(laptop_id=laptop.id, price_rm=price_rm, source="apple_my_edu",
                               source_url="https://www.apple.com/my-edu/shop/buy-mac/macbook-pro/x"))
    session.commit()


@pytest.fixture
def edu(session, m6):
    _store_edu(session, m6)
    return m6


@pytest.fixture
def signed_in(api_user):
    """The public reads see the caller: api_client only signs in the routes that require it."""
    from app.main import app
    from app.users.auth import get_optional_user

    app.dependency_overrides[get_optional_user] = lambda: api_user
    yield api_user
    app.dependency_overrides.pop(get_optional_user, None)


def _price_factor(score: dict) -> float:
    return next(b["raw_score"] for b in score["breakdown"] if b["factor"] == "price")


# --------------------------------------------------------------------------- the toggle's reads

def test_edu_price_gives_the_price_its_date_and_scores_calculated_from_it(api_client, edu):
    body = api_client.get(f"/api/v2/laptops/{edu.id}/edu-price").json()["edu"]
    assert (body["price_rm"], body["official_price_rm"], body["saving_rm"]) == (7899.0, 8299.0, 400.0)
    assert body["source"] == "apple_my_edu" and body["checked_at"] and "/my-edu/" in body["source_url"]
    official = api_client.get(f"/api/v2/laptops/{edu.id}/configure").json()
    assert len(body["scores"]) == len(official["scores"]) == 5
    for at_edu, at_official in zip(body["scores"], official["scores"]):
        assert _price_factor(at_edu) > _price_factor(at_official), at_edu["use_case"]
    assert body["personalized_score"] is None, "the caller has no preference profile"


def test_a_laptop_with_no_edu_price_to_show_answers_null_not_an_error(api_client, session, m6):
    url = f"/api/v2/laptops/{m6.id}/edu-price"
    assert api_client.get(url).json() == {"laptop_id": str(m6.id), "edu": None}

    _store_edu(session, m6, price_rm=8299.0)   # not below the official price
    assert api_client.get(url).json()["edu"] is None

    row = session.exec(select(LaptopEduPrice)).one()
    row.price_rm = 7899.0
    m6.status = "inactive"
    session.add_all([row, m6])
    session.commit()
    assert api_client.get(url).json()["edu"] is None
    assert api_client.get("/api/v2/laptops/00000000-0000-0000-0000-000000000000/edu-price").status_code == 404


def test_the_personalized_ring_compares_the_budget_with_the_edu_price(api_client, session, signed_in, edu):
    """A budget of RM 8,000: the official RM 8,299 is over it, the Edu RM 7,899 is not."""
    session.add(LaptopUserPreference(user_id=signed_in.id, budget={"min": 0, "max": 8000}))
    session.commit()
    at_edu = api_client.get(f"/api/v2/laptops/{edu.id}/edu-price").json()["edu"]["personalized_score"]
    at_official = api_client.get(f"/api/v2/laptops/{edu.id}/configure").json()["personalized_score"]
    assert at_edu["mode"] == at_official["mode"] == "personalized"
    assert _price_factor(at_edu) > _price_factor(at_official)
    assert at_edu == api_client.get(f"/api/v2/laptops/{edu.id}/configure?edu=true").json()["personalized_score"]


def test_configure_with_edu_totals_the_education_stores_numbers(api_client, edu):
    official = api_client.get(f"/api/v2/laptops/{edu.id}/configure?memory=32gb&storage=4tb").json()
    assert (official["total_rm"], official["standard_price_rm"], official["edu"]) == (16849.0, 8299.0, False)

    body = api_client.get(f"/api/v2/laptops/{edu.id}/configure?memory=32gb&storage=4tb&edu=true").json()
    assert body["edu"] is True and body["standard_price_rm"] == 7899.0
    assert body["total_rm"] == round(7899.0 + (16849.0 - 8299.0) * 0.9, 2)
    assert sum(a["add_on_rm"] for a in body["add_ons"]) == round((16849.0 - 8299.0) * 0.9, 2)

    other = api_client.get(f"/api/v2/laptops/{edu.id}/configure?chip=m5pro-15-16&edu=true").json()
    assert other["chip_add_on_rm"] == (10299.0 - 400) - 7899.0


def test_options_with_edu_lists_only_kits_the_education_store_priced(api_client, edu):
    official = api_client.get(f"/api/v2/laptops/{edu.id}/options").json()
    body = api_client.get(f"/api/v2/laptops/{edu.id}/options?edu=true").json()
    assert body["edu"] is True and body["standard_price_rm"] == 7899.0 and official["edu"] is False
    assert {k["chip"] for k in official["kits"]} - {k["chip"] for k in body["kits"]} == {UNFETCHED}
    kit = next(k for k in body["kits"] if k["chip"] == "m6-12-12")
    assert kit["kit_price_rm"] == 7899.0 and kit["chip_add_on_rm"] == 0.0
    assert api_client.get(f"/api/v2/laptops/{edu.id}/configure?chip={UNFETCHED}&edu=true").status_code == 422


def test_edu_is_a_409_when_the_price_or_the_laptops_own_kit_is_not_stored(api_client, session, m6):
    url = f"/api/v2/laptops/{m6.id}/configure?edu=true"
    resp = api_client.get(url)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "no_edu_price"

    _store_edu(session, m6)
    assert api_client.get(url).status_code == 200
    for option in session.exec(select(LaptopOption).where(LaptopOption.chip == "m6-12-12")):
        option.edu_fetched_at = None
        session.add(option)
    session.commit()
    assert api_client.get(url).status_code == 409
    assert api_client.get(f"/api/v2/laptops/{m6.id}/options?edu=true").status_code == 409
    assert api_client.get(f"/api/v2/laptops/{m6.id}/configure").status_code == 200, "the official price is untouched"


def test_deleting_a_laptop_takes_its_edu_price_with_it(session, edu):
    laptop_router.delete_laptop(edu.id, session)
    assert session.get(Laptop, edu.id) is None
    assert session.exec(select(LaptopEduPrice)).all() == []


# --------------------------------------------------------------------------- the guard, second half

def _everything_else(session, api_client, monkeypatch) -> dict:
    """What must not know an Edu price exists. A budget of RM 8,000 sits between the
    M6's official RM 8,299 and its Edu RM 7,899."""
    monkeypatch.setattr(retrieval, "_get_query_vector", lambda q: (_ for _ in ()).throw(RuntimeError("down")))
    generate_all_pick_scores(session)
    session.expire_all()
    return {
        "ranges": json.dumps(get_laptop_ranges(session), sort_keys=True, default=str),
        "stored_scores": sorted((str(s.laptop_id), s.use_case, s.score, json.dumps(s.breakdown, sort_keys=True))
                                for s in session.exec(select(LaptopPickScore))),
        "ranking": [(str(laptop.id), score.score) for score, laptop, _ in
                    get_ranking_for_use_case(session, "general_use", limit=10)],
        "pico_within_budget": sorted(str(c.laptop.id) for c in
                                     retrieval.retrieve_candidates("a laptop", session, budget_max=8000)),
        "list_within_budget": sorted(row["id"] for row in
                                     api_client.get("/api/v2/laptops/?price_max=8000").json()),
        "official_configure": api_client.get("/api/v2/laptops/%s/configure" % session.exec(
            select(Laptop.id).where(Laptop.price_rm == 8299.0)).one()).json(),
    }


def test_an_edu_price_changes_nothing_outside_the_toggle(session, api_client, monkeypatch, m6):
    before = _everything_else(session, api_client, monkeypatch)
    assert str(m6.id) not in before["pico_within_budget"] and str(m6.id) not in before["list_within_budget"]
    assert len(before["pico_within_budget"]) == len(before["list_within_budget"]) == 1

    _store_edu(session, m6)
    assert api_client.get(f"/api/v2/laptops/{m6.id}/edu-price").json()["edu"]["price_rm"] == 7899.0
    after = _everything_else(session, api_client, monkeypatch)
    assert after == before
    assert session.get(Laptop, m6.id).price_rm == 8299.0


# --------------------------------------------------------------------------- the fetch

@pytest.fixture
def air13(session):
    """The Air's stored buy and specs pages, and its two 13-inch standard configs."""
    apple = _apple(session)

    def raw(url, name, dump):
        session.add(RawScrapLaptop(source_url=url, brand_id=apple.id, raw_product_name=name, raw_prices=[],
                                   raw_specs_dump=dump))

    raw(AIR_BUY, "Buy MacBook Air", {"buy_page": {"url": AIR_BUY, "specs_url": AIR_SPECS, "configs": [
        {"size": 13, "chip": "M5", "cpu_cores": 10, "gpu_cores": 8, "ram_gb": 16, "ssd_gb": 512,
         "slugs": [AIR13_SLUG, "another-colour"], "part_numbers": ["MDH74ZP/A", "MDHE4ZP/A"]},
        {"size": 13, "chip": "M5", "cpu_cores": 10, "gpu_cores": 10, "ram_gb": 16, "ssd_gb": 1024,
         "slugs": [AIR13_1010_SLUG], "part_numbers": ["MDHA4ZP/A"]}]}})
    raw(AIR_SPECS, "MacBook Air", {"scraped_features": [fixture("apple_macbook_air_specs.txt")]})
    base = make_laptop(apple.id, product_name="MacBook Air 13", price_rm=5499.0, part_numbers=["MDHE4ZP/A", "MDH74ZP/A"])
    session.add(base)
    session.commit()
    return base


class _BothStores:
    """The main store, and the education store unless it is down."""

    def __init__(self, edu_down=False):
        self.main, self.edu, self.edu_down = FakeApple(), FakeAppleEdu(), edu_down

    def __call__(self, url: str) -> str:
        if "/my-edu/" not in url:
            return self.main(url)
        if self.edu_down:
            raise fetch.Blocked(f"HTTP 403 from {url}")
        return self.edu(url)


def _options(session):
    session.expire_all()
    return {(o.chip, o.option_group, o.value): o for o in session.exec(select(LaptopOption))}


def test_the_options_step_then_fetches_the_education_store(session, air13):
    get = _BothStores()
    result = asyncio.run(fetch.run_options_step(session, get=get, pause_s=0))
    edu = result["edu"]
    assert edu["prices"] == 1 and edu["chip_kits"] == 2 and edu["stopped"] is None and edu["problem"] is None
    assert len(edu["no_laptop"]) == 1 and AIR13_1010_SLUG in edu["no_laptop"][0], "no laptop row has that part number"
    assert edu["not_below_official"] == [] and edu["apple_calls"] == len(get.edu.calls)
    assert len(get.edu.calls) == 2 + (len(get.main.calls)), "one page per config, then the same kit calls"

    [price] = session.exec(select(LaptopEduPrice)).all()
    assert (price.laptop_id, price.price_rm, price.source) == (air13.id, 5099.0, "apple_my_edu")
    assert price.source_url == fetch.edu_url(f"{AIR_BUY}/{AIR13_SLUG}"), "one colour per configuration"

    options = _options(session)
    assert edu["option_rows"] == len(options) and all(o.edu_fetched_at for o in options.values())
    base = options[("10-8", "chip", "10-8")]
    assert (base.kit_price_rm, base.edu_kit_price_rm) == (5499.0, 5099.0), "the official price is not touched"
    assert session.get(Laptop, air13.id).price_rm == 5499.0


def test_a_failed_edu_pass_keeps_the_stored_edu_numbers_and_every_official_one(session, air13):
    asyncio.run(fetch.run_options_step(session, get=_BothStores(), pause_s=0))
    first = {key: (o.add_on_rm, o.kit_price_rm, o.edu_add_on_rm, o.edu_kit_price_rm, o.edu_fetched_at)
             for key, o in _options(session).items()}
    checked = session.exec(select(LaptopEduPrice)).one().checked_at

    result = asyncio.run(fetch.run_options_step(session, get=_BothStores(edu_down=True), pause_s=0))
    assert "403" in result["edu"]["stopped"]["reason"] and result["edu"]["prices"] == 0
    assert "Apple stopped the education-store fetch" in result["edu"]["problem"]
    assert result["chip_kits"] == 2 and result["stopped"] is None, "the main store's fetch is not the Edu pass's"
    again = {key: (o.add_on_rm, o.kit_price_rm, o.edu_add_on_rm, o.edu_kit_price_rm, o.edu_fetched_at)
             for key, o in _options(session).items()}
    assert again == first, "the rows were replaced by the main pass and kept their Edu numbers"
    session.expire_all()
    assert session.exec(select(LaptopEduPrice)).one().checked_at == checked


def test_a_second_check_stamps_the_date_and_a_new_price_its_own(session, air13):
    asyncio.run(fetch.run_options_step(session, get=_BothStores(), pause_s=0))
    row = session.exec(select(LaptopEduPrice)).one()
    row.price_rm, old = 4999.0, datetime(2026, 1, 1, tzinfo=timezone.utc)
    row.checked_at = row.price_changed_at = old
    session.add(row)
    session.commit()

    asyncio.run(fetch.run_options_step(session, get=_BothStores(), pause_s=0))
    session.expire_all()
    row = session.exec(select(LaptopEduPrice)).one()
    assert row.price_rm == 5099.0 and row.checked_at > old and row.price_changed_at > old
