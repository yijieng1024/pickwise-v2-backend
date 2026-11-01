"""Guide extraction on real pages. The fixtures are pdfplumber word lists dumped
from the Q3 2026 guides, and expected.json was checked by hand against the printed
pages (2026-11-01). They hold text and coordinates only, no PDF."""
import ast
import json
from pathlib import Path

import pytest

from ._adapters import extract_price_guide as guide

FIXTURES = Path(__file__).parent / "fixtures" / "price_guide"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def _page(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _by_sku(words: list[dict]) -> dict[str, dict]:
    return {r["sku"]: r for r in guide.pair_skus_with_prices(words)}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_hand_checked_page_comes_out_as_printed(name):
    found = {
        sku: [r["price_rm"], r["cpu"], r["ram_gb"], r["ssd_gb"]]
        for sku, r in _by_sku(_page(name)).items()
    }
    assert {sku: v for sku, v in found.items() if not guide.NOT_A_LAPTOP.match(sku)} == EXPECTED[name]


def test_a_sku_with_no_price_in_its_column_does_not_take_the_neighbours():
    words = [w for w in _page("rog_p83") if w["text"] != "RM5,999"]

    rows = _by_sku(words)

    assert rows["FA608U-HRV068W"]["price_rm"] is None  # RM5,999 was its own
    assert rows["FA608U-HITU066WP"]["price_rm"] == 6299.0


def test_colour_variants_side_by_side_share_their_column():
    rows = _by_sku(_page("consumer_p84"))

    assert {rows[s]["price_rm"] for s in ("A1504V-ABQ268WS", "A1504V-ABQ269WS", "A1504V-ABQ270WS")} == {4199.0}
    assert rows["A1504V-ABQ269WS"]["ram_gb"] == 16


def test_the_facing_page_at_negative_x_is_not_read():
    words = _page("rog_p75")
    assert any(w["x0"] < 0 and w["text"] == "GX651A-XSR222W" for w in words)  # it is really there

    assert "GX651A-XSR222W" not in _by_sku(words)


def test_a_bare_number_is_a_price_only_on_the_price_row():
    words = _page("consumer_p77")
    assert _by_sku(words)["UX3407N-AQD881WS"]["price_rm"] == 7499.0  # printed as "7499"

    moved = [{**w, "top": w["top"] + 40} if w["text"] == "7499" else w for w in words]

    assert _by_sku(moved)["UX3407N-AQD881WS"]["price_rm"] is None


def test_two_drives_add_up():
    assert _by_sku(_page("rog_p75"))["G835L-XGTQ284WP"]["ssd_gb"] == 2048  # "1TB + 1TB PCIe"


def test_an_sku_wrapped_onto_two_lines_is_joined():
    assert _by_sku(_page("business_p8"))["BM3406CGA-LY0051X"]["price_rm"] == 8799.0


@pytest.mark.parametrize("sku", ["PM3406CHAP-LY0268WS", "PM1503CDA-S70516WS", "BM1403CDA-S61803X"])
def test_expertbook_laptops_are_not_mistaken_for_desktops(sku):
    assert not guide.NOT_A_LAPTOP.match(sku)


@pytest.mark.parametrize("sku", ["PM700MG-B5150X", "PM500MH-0R5220014X", "D700MF-B5150X", "CX5403CM-AQM1393", "V501SV-05210H010WS"])
def test_desktops_and_chromebooks_are_skipped(sku):
    assert guide.NOT_A_LAPTOP.match(sku)


def test_a_brochure_page_is_not_a_price_page():
    words = [
        {"text": "From", "x0": 10, "x1": 30, "top": 10},
        {"text": "RM5,999", "x0": 35, "x1": 70, "top": 10},
    ]
    assert not guide.is_price_page(words)


def test_a_sku_printed_twice_keeps_one_row_on_the_first_page():
    page = _page("rog_p79")

    rows, summary = guide.extract_rows([[], page, page])

    assert [r["sku"] for r in rows if r["sku"] == "GZ302E-ARU007W"] == ["GZ302E-ARU007W"]
    assert rows[0]["page"] == 2
    assert [s["page"] for s in summary] == [2, 3]


def test_the_script_imports_no_database_and_no_network():
    tree = ast.parse(Path(guide.__file__).read_text(encoding="utf-8"))
    top_level = {
        (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
        for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    assert top_level <= {"argparse", "hashlib", "json", "re", "pathlib", "typing"}
