"""Education prices (step 10d): the education-store pass of the Apple options fetch, and
the configurator priced from its numbers. No database, no network: Apple is
tests/_apple_fake.py, the kits are the ones stored in production on 2026-11-04."""
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

from tests._apple_fake import AIR13_1010_SLUG, AIR13_SLUG, AIR_BUY, FakeAppleEdu, fixture

from ._adapters import apple_options, apple_options_fetch, options_service
from .test_apple_configure import PRO14, SCORING
from .test_apple_options import _air

ROWS = json.loads((Path(__file__).parent / "fixtures" / "apple_kits_pro_2026-11-04.json").read_text(encoding="utf-8"))
PRO14_ROWS = [r for r in ROWS if r["size_inch"] == 14 and r["line"] == "liquid-retina"]
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)


def _with_edu(rows, but=()):
    """The rows as after an Edu pass: every add-on 10% lower, every kit RM 400 lower."""
    return [r if r["chip"] in but else {
        **r, "edu_fetched_at": NOW,
        "edu_add_on_rm": None if r["add_on_rm"] is None else round(r["add_on_rm"] * 0.9, 2),
        "edu_kit_price_rm": None if r["kit_price_rm"] is None else r["kit_price_rm"] - 400} for r in rows]


def test_the_education_store_is_the_same_path_under_my_edu():
    assert apple_options_fetch.edu_url(f"{AIR_BUY}/{AIR13_SLUG}") == \
        f"https://www.apple.com/my-edu/shop/buy-mac/macbook-air/{AIR13_SLUG}"
    already = "https://www.apple.com/my-edu/shop/api/cto/update-config?x=1"
    assert apple_options_fetch.edu_url(already) == already


def test_the_configure_page_parser_reads_the_education_stores_endpoints():
    page = fixture("apple_configure_air13.html").replace("/my/shop/api/cto/", "/my-edu/shop/api/cto/")
    configure = apple_options.parse_configure_page(page)
    assert configure.update_path.startswith("/my-edu/shop/api/cto/update-config")
    assert configure.switcher_path.startswith("/my-edu/shop/api/cto/dependency-switcher")


def test_the_edu_pass_asks_only_the_education_store_and_takes_a_price_only_for_its_own_part_number():
    fake = FakeAppleEdu()  # raises on any URL outside /my-edu/
    configs = [{"url": f"{AIR_BUY}/{AIR13_SLUG}", "part_numbers": ["MDH74ZP/A", "MDHA4ZP/A"]},
               {"url": f"{AIR_BUY}/{AIR13_1010_SLUG}", "part_numbers": ["MDHA4ZP/A"]},
               {"url": "https://www.apple.com/my/shop/buy-mac/macbook-neo/silver-256gb", "part_numbers": ["MHFJ4ZP/A"]}]
    edu = apple_options_fetch.fetch_edu(configs, [_air()], fake, pause_s=0)
    assert [(p["price_rm"], p["url"].split("/")[3]) for p in edu["prices"]] == [(5099.0, "my-edu"), (6449.0, "my-edu")]
    [failed] = edu["prices_failed"]
    assert "macbook-neo" in failed["url"] and "MDH74ZP/A" in failed["reason"]
    assert {k["chip"]: k["rows"][0]["kit_price_rm"] for k in edu["kits"]} == {"10-8": 5099.0, "10-10": 6449.0}
    assert edu["stopped"] is None and edu["calls"] == len(fake.calls)


def test_a_block_on_a_price_page_stops_the_edu_pass_before_the_kits():
    def blocked(url):
        raise apple_options_fetch.Blocked(f"HTTP 403 from {url}")

    edu = apple_options_fetch.fetch_edu(
        [{"url": f"{AIR_BUY}/{AIR13_SLUG}", "part_numbers": ["MDH74ZP/A"]}], [_air()], blocked, pause_s=0)
    assert edu["prices"] == [] and edu["kits"] == []
    assert "403" in edu["stopped"]["reason"] and edu["calls"] == 1


def test_a_kit_the_edu_pass_did_not_fill_completely_offers_no_edu_price():
    assert options_service.edu_rows(PRO14_ROWS) == [], "never fetched: nothing is offered"
    rows = _with_edu(PRO14_ROWS, but=("m5max-18-40",))
    one_missing = [dict(r, edu_fetched_at=None) if (r["chip"], r["value"]) == ("m5pro-15-16", "48gb") else r
                   for r in rows]
    kits = apple_options.kits_from_rows(options_service.edu_rows(one_missing))
    assert set(kits) == {"m6-12-12", "m5pro-18-20", "m5max-18-32"}


def test_an_edu_total_is_the_edu_kit_plus_the_edu_add_ons():
    kits = apple_options.kits_from_rows(options_service.edu_rows(_with_edu(PRO14_ROWS)))
    q = apple_options.quote(kits, "m6-12-12", {"memory": "32gb", "storage": "4tb"})
    # Official: 8,299 + 8,550 of add-ons = 16,849 (Apple's own total).
    assert q.total_rm == round(8299 - 400 + 8550 * 0.9, 2)


def test_the_official_kits_are_the_same_whether_or_not_the_edu_columns_are_filled():
    """What Pico's tool, the admin page and every toggle-off read."""
    assert apple_options.kits_from_rows(_with_edu(PRO14_ROWS)) == apple_options.kits_from_rows(PRO14_ROWS)


def test_the_ring_at_the_edu_price_differs_only_in_its_price_factor():
    official = options_service.laptop_to_scorable(PRO14, "Apple")
    at_official, _ = options_service._use_case_scores(official, SCORING)
    at_edu, _ = options_service._use_case_scores(dataclasses.replace(official, price=4000.0), SCORING)
    for a, b in zip(at_official, at_edu):
        changed = {x["factor"] for x, y in zip(a["breakdown"], b["breakdown"]) if x["raw_score"] != y["raw_score"]}
        assert changed == {"price"}, a["use_case"]
        assert b["score"] > a["score"], "a lower price scores higher"
