"""An education price is read by the details page's Edu toggle and by nothing else
(step 10d, Jack 2026-11-10): never by Pico, rankings, use-case cards, budget filters,
the scoring ranges or generate-all.

The first half of that guard: no module outside the list below may name an Edu price at
all, in any spelling. A new reader has to be added here, in a diff someone reads. The
second half, that those paths return the same with and without an Edu price stored, is
tests/integration/test_edu_prices.py."""
import re
from pathlib import Path

APP = Path(__file__).parents[2] / "app"
_EDU = re.compile(r"(?i)(?<![a-z])edu(?![a-z])|EduPrice")

MAY_NAME_AN_EDU_PRICE = {
    "laptops/laptop_models.py",          # the laptop_edu_prices table
    "laptops/option_model.py",           # the edu_ columns of laptop_options
    "laptops/options_service.py",        # the only reader
    "laptops/options_router.py",         # GET /laptops/{id}/edu-price and ?edu=true
    "scraper/apple_options_fetch.py",    # the only writer
    "processor/apple_options.py",        # the configure page's /my-edu/ endpoint pattern
    "admin/apple_options_router.py",     # passes the fetch's `edu` summary through
}


def test_only_the_edu_modules_name_an_edu_price():
    naming = {path.relative_to(APP).as_posix() for path in APP.rglob("*.py")
              if _EDU.search(path.read_text(encoding="utf-8"))}
    assert naming - MAY_NAME_AN_EDU_PRICE == set(), "a new reader of the Edu price: is it the details page?"
    assert MAY_NAME_AN_EDU_PRICE - naming == set(), "the allow-list names a module that no longer needs it"


def test_the_pattern_sees_every_spelling_and_not_ordinary_words():
    for text in ("LaptopEduPrice", "edu_add_on_rm", "edu=True", "/my-edu/", "laptop_edu_prices", '"edu"'):
        assert _EDU.search(text), text
    for text in ("education", "schedule", "reduce", "educator"):
        assert not _EDU.search(text), text
