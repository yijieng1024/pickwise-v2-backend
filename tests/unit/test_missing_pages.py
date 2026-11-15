"""The pages a crawl no longer finds (plan step 11, decision 8): one page reached by
several paths counts once, and Chromebooks are left out (decision of 2026-10-27)."""
from tests.unit._adapters import bulk_scraper

A14 = "https://www.asus.com/my/laptops/for-gaming/tuf-gaming/asus-tuf-gaming-a14-2026-fa401ea"


def test_one_page_by_several_paths_counts_once():
    targets = [f"{A14}/spec", f"{A14}/techspec", f"{A14}/techspec/", f"{A14}/"]
    assert bulk_scraper.missing_urls(targets, ["https://rog.asus.com/my/other/"]) == [f"{A14}/spec"]


def test_a_page_the_crawl_found_by_another_path_is_not_missing():
    assert bulk_scraper.missing_urls([f"{A14}/techspec/", "https://www.hp.com/my-en/shop/x.html"],
                                     [f"{A14}?v=2", "https://www.hp.com/my-en/shop/x.html"]) == []


def test_chromebooks_are_left_out():
    targets = ["https://www.asus.com/my/laptops/for-home/chromebook/asus-chromebook-plus-cx34-cx3402/",
               "https://www.asus.com/my/laptops/for-home/vivobook/asus-vivobook-16-m1607/"]
    assert bulk_scraper.missing_urls(targets, [A14]) == [targets[1]]
