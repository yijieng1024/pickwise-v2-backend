"""GET /laptops/?added_from=&added_to= filters on created_at (the admin catalog's Added filter)."""
from datetime import datetime

import pytest

from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration


def test_added_filter_bounds_created_at(api_client, session, brand):
    old = make_laptop(brand.id, model_code="added-old", created_at=datetime(2026, 6, 22, 8, 0))
    new = make_laptop(brand.id, model_code="added-new", created_at=datetime(2026, 11, 3, 6, 53))
    session.add_all([old, new])
    session.commit()

    def codes(**params):
        r = api_client.get("/api/v2/laptops/", params={"brand_id": str(brand.id), **params})
        assert r.status_code == 200, r.text
        return {row["model_code"] for row in r.json()}

    assert codes(added_from="2026-11-03T00:00:00") == {"added-new"}
    assert codes(added_to="2026-11-02T23:59:59") == {"added-old"}
    # Inclusive at both ends, in the frontend's format (UTC, no zone)
    assert codes(added_from="2026-11-03T06:53:00", added_to="2026-11-03T06:53:00") == {"added-new"}
