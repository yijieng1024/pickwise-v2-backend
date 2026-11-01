"""PassMark table parsing on real rows, and the floor that keeps a challenge page
from counting as a refresh. Writing is in tests/integration/test_benchmark_refresh.py."""
from pathlib import Path

import pytest

from ._adapters import passmark_scraper

FIXTURES = Path(__file__).parent / "fixtures" / "scraper"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _table(rows: str) -> str:
    return f'<TABLE ID="cputable"><thead><tr><th>Name</th></tr></thead><tbody>{rows}</tbody></TABLE>'


@pytest.mark.parametrize("fixture, name, mark", [
    ("passmark_cpu.html", "100-000001020", 16146),
    ("passmark_gpu.html", "256MB DDR Radeon 9800 XT", 37),
])
def test_real_rows_give_name_and_mark_pairs(fixture, name, mark):
    rows = passmark_scraper.parse_passmark_table(_read(fixture))

    assert len(rows) == 300
    assert rows[0] == (name, mark)
    assert all(isinstance(n, str) and n and isinstance(m, int) for n, m in rows)


def test_commas_are_stripped_and_upper_case_tags_are_found():
    rows = passmark_scraper.parse_passmark_table(_table("<tr><TD>Chip</TD><TD>16,146</TD></tr>"))
    assert rows == [("Chip", 16146)]


def test_a_row_with_no_numeric_mark_is_skipped():
    rows = passmark_scraper.parse_passmark_table(_table(
        "<tr><td>No score</td><td>NA</td></tr><tr><td>Short row</td></tr><tr><td>Good</td><td>5</td></tr>"
    ))
    assert rows == [("Good", 5)]


def test_the_header_row_is_not_a_row():
    html = '<table id="cputable"><thead><tr><td>Name</td><td>123</td></tr></thead><tbody></tbody></table>'
    assert passmark_scraper.parse_passmark_table(html) == []


@pytest.mark.parametrize("source, fixture", [("cpu", "passmark_cpu.html"), ("gpu", "passmark_gpu.html")])
def test_a_list_below_the_floor_raises_and_never_reaches_the_database(monkeypatch, source, fixture):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: _read(fixture))
    # No session and no progress to reach: going past the floor is an AttributeError
    with pytest.raises(ValueError, match="below the floor"):
        passmark_scraper.refresh(source, None, None)


def test_a_challenge_page_returning_200_raises(monkeypatch):
    monkeypatch.setattr(passmark_scraper, "fetch_html", lambda url: "<html>Just a moment...</html>")
    with pytest.raises(ValueError, match="has 0 rows"):
        passmark_scraper.refresh("cpu", None, None)


def test_passmarks_own_date_is_read_when_the_page_gives_one():
    assert passmark_scraper._source_updated_text(_read("passmark_cpu.html")) == (
        "Last updated on the 1st of November 2026"
    )
    assert passmark_scraper._source_updated_text("<html></html>") is None
