"""SCRAPER_BRANDS must name exactly the brands _dispatch_scraper routes."""
import inspect
import re

from ._adapters import bulk_scraper


def test_scraper_brands_match_the_dispatch():
    dispatched = set(re.findall(r'name == "([a-z]+)"', inspect.getsource(bulk_scraper._dispatch_scraper)))
    assert dispatched == set(bulk_scraper.SCRAPER_BRANDS)
    assert bulk_scraper.HTML_UPLOAD_BRANDS <= bulk_scraper.SCRAPER_BRANDS
