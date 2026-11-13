"""GET /laptops/?price_checked= filters on price_checked_at (the admin catalog's Last Checked filter)."""
from datetime import datetime

import pytest

from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def test_price_checked_filter(api_client, session, brand):
    checked = make_laptop(brand.id, model_code="checked")
    checked.price_checked_at = datetime(2026, 11, 3, 6, 53)
    never = make_laptop(brand.id, model_code="never-checked")
    never.price_checked_at = None
    session.add_all([checked, never])
    session.commit()

    def codes(**params):
        r = api_client.get("/api/v2/laptops/", params={"brand_id": str(brand.id), **params})
        assert r.status_code == 200, r.text
        assert r.headers["X-Total-Count"] == str(len(r.json()))
        return {row["model_code"] for row in r.json()}

    assert codes(price_checked="false") == {"never-checked"}
    assert codes(price_checked="true") == {"checked"}
    assert codes() == {"checked", "never-checked"}
