"""A page counts as already scraped only by its own URL or URL?v=N, never by a longer
URL it is a prefix of: .../buy-mac/macbook-pro was skipped because the PROLED buy page,
.../buy-mac/macbook-pro-proled-xdr, was stored."""
import pytest

from app.laptops.brand_model import LaptopBrand
from app.scraper.bulk_scraper import stored_rows_of
from app.scraper.models import RawScrapLaptop
from sqlmodel import select

pytestmark = pytest.mark.integration

PRO = "https://www.apple.com/my/shop/buy-mac/macbook-pro"


def test_prefix_url_is_not_already_scraped(session):
    brand = LaptopBrand(name="Apple", base_scrape_url="https://example.invalid")
    session.add(brand)
    session.commit()
    for url in (PRO + "-proled-xdr", "https://x.invalid/asus?v=2"):
        session.add(RawScrapLaptop(source_url=url, brand_id=brand.id, raw_product_name="x", raw_specs_dump={}))
    session.commit()

    assert session.exec(select(RawScrapLaptop).where(stored_rows_of(PRO))).first() is None
    assert session.exec(select(RawScrapLaptop).where(stored_rows_of("https://x.invalid/asus"))).first() is not None
