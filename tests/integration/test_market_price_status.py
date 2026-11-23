"""The price tool's catalog layer finds what lookup_laptop finds (ADR-0009 Amendment II):
active and discontinued rows, never inactive or suspended, by name or by model code. A
discontinued row carries its last official price and date, never official_price_rm."""
from datetime import datetime, timezone

import pytest

from app.agent.tools import market_price
from app.laptops.laptop_models import LaptopPriceHistory
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

# Names far enough apart that one can never fuzzy-match another above the floor.
NAMES = {"active": "Quillon Aurora 14", "discontinued": "Brisket Nimbus 16",
         "inactive": "Tamarind Vexa 13", "suspended": "Ocelot Fjord 15"}


@pytest.fixture
def one_of_each(session, brand):
    laptops = {}
    for status, name in NAMES.items():
        laptops[status] = make_laptop(brand.id, product_name=name, model_code=f"mp-{status}", status=status,
                                      price_rm=5999.0, price_checked_at=None)
        session.add(laptops[status])
    session.commit()
    session.add(LaptopPriceHistory(laptop_id=laptops["discontinued"].id, price_rm=5999.0, source="page",
                                   recorded_at=datetime(2026, 8, 15, tzinfo=timezone.utc)))
    session.commit()
    return laptops


@pytest.mark.parametrize("status", ["inactive", "suspended"])
def test_inactive_and_suspended_are_never_found(session, one_of_each, status):
    assert market_price._catalog_lookup(NAMES[status], None, session) == {"found": False}
    assert market_price._catalog_lookup(NAMES[status], f"mp-{status}", session) == {"found": False}


def test_an_active_row_is_found_as_before(session, one_of_each):
    found = market_price._catalog_lookup(NAMES["active"], "mp-active", session)
    assert (found["found"], found["official_price_rm"], found["match_score"]) == (True, 5999.0, 100)
    assert "no_longer_sold" not in found


def test_a_discontinued_row_carries_its_last_official_price_not_a_current_one(session, one_of_each):
    found = market_price._catalog_lookup(NAMES["discontinued"], None, session)
    assert found["status"] == "discontinued" and found["no_longer_sold"] is True
    assert (found["last_official_price_rm"], found["last_official_price_date"]) == (5999.0, "2026-08-15")
    assert "official_price_rm" not in found and "price_history" not in found
