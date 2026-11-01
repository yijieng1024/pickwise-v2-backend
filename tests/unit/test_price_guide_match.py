"""The decision of which catalog row a guide row prices. Nothing unclear is written."""
import pytest

from ._adapters import price_guide

CatalogRow, GuideRow = price_guide.CatalogRow, price_guide.GuideRow
cpu_part, match = price_guide.cpu_part, price_guide.match


def guide(sku="P1403CVA-S61159X", price=4399.0, cpu="Intel Core i5-13420H", ram=8, ssd=512, model_code=None):
    return GuideRow(sku=sku, model_code=model_code or sku.split("-")[0], price_rm=price, cpu=cpu, ram_gb=ram, ssd_gb=ssd)


def catalog(id="a", model_code="asus-expertbook-p1-p1403cva", name="ASUS ExpertBook P1 P1403CVA",
            cpu="Intel Core i5-13420H", ram=8, ssd=512, sku=None):
    return CatalogRow(id=id, model_code=model_code, product_name=name, processor_model=cpu,
                      ram_gb=ram, ssd_gb=ssd, manufacturer_sku=sku)


def test_an_exact_sku_matches_whatever_the_specs_say():
    laptop = catalog(sku="g835l-wgsa478wp ", model_code="rog-strix", name="ROG Strix SCAR 18", cpu="Unknown")

    result = match([guide(sku="G835L-WGSA478WP", cpu=None, ram=None, ssd=None)], [laptop])

    assert [(c.id, [r.sku for r in rows]) for c, rows in result.matched] == [("a", ["G835L-WGSA478WP"])]


def test_model_code_and_specs_match():
    result = match([guide()], [catalog()])

    assert [c.id for c, _ in result.matched] == ["a"]
    assert result.unmatched_guide == [] and result.uncovered == []


def test_the_base_model_code_is_the_fallback():
    laptop = catalog(model_code="asus-expertbook-p1403", name="ASUS ExpertBook P1 (P1403)")

    assert [c.id for c, _ in match([guide()], [laptop]).matched] == ["a"]


def test_the_base_code_does_not_match_inside_another_code():
    laptop = catalog(model_code="asus-gp14035", name="ASUS GP14035")

    assert match([guide()], [laptop]).matched == []


def test_colour_variants_at_one_price_count_as_matched():
    black, silver = guide(sku="P1403CVA-S61159X"), guide(sku="P1403CVA-S61160X")

    result = match([black, silver], [catalog()])

    assert [len(rows) for _, rows in result.matched] == [2]
    assert result.conflict == []


def test_two_catalog_rows_that_fit_are_ambiguous_and_list_both():
    first, second = catalog(id="a"), catalog(id="b", model_code="asus-expertbook-p1-p1403cva-oled")

    result = match([guide()], [first, second])

    assert result.matched == []
    assert [[c.id for c in candidates] for _, candidates in result.ambiguous] == [["a", "b"]]
    assert result.uncovered == []  # they are listed as candidates, not as "no guide row"


def test_two_guide_prices_for_one_row_conflict():
    result = match([guide(sku="P1403CVA-A1", price=4399.0), guide(sku="P1403CVA-A2", price=4599.0)], [catalog()])

    assert result.matched == []
    assert [(c.id, sorted(r.price_rm for r in rows)) for c, rows in result.conflict] == [("a", [4399.0, 4599.0])]


def test_a_cpu_differing_only_by_suffix_is_not_a_match():
    result = match([guide(cpu="Intel Core i7-13620H")], [catalog(cpu="Intel Core i7-13620HX")])

    assert result.matched == []
    assert len(result.unmatched_guide) == 1


@pytest.mark.parametrize("field, value", [("ram", 16), ("ssd", 1024)])
def test_different_memory_or_storage_is_not_a_match(field, value):
    assert match([guide()], [catalog(**{field: value})]).matched == []


@pytest.mark.parametrize("gap", [{"cpu": None}, {"ram": None}, {"ssd": None}])
def test_a_guide_row_with_a_spec_missing_cannot_use_the_spec_path(gap):
    # The catalog row has the same gap: unknown must not equal unknown
    result = match([guide(**gap)], [catalog(**gap)])

    assert result.matched == []
    assert len(result.unmatched_guide) == 1


def test_a_guide_row_with_no_price_is_unpriced_and_claims_nothing():
    result = match([guide(price=None)], [catalog()])

    assert len(result.unpriced) == 1
    assert [c.id for c in result.uncovered] == ["a"]


def test_a_catalog_row_no_guide_covers_needs_a_price_by_hand():
    other = catalog(id="b", model_code="asus-vivobook-e1404", name="ASUS Vivobook Go 14 E1404F")

    assert [c.id for c in match([guide()], [catalog(), other]).uncovered] == ["b"]


@pytest.mark.parametrize("left, right", [
    ("Intel Core i7 Processor 14650HX", "Intel Core i7-14650HX"),
    ("Intel Core Ultra 7 Processor 255H", "Intel Core Ultra 7 255H"),
    ("Intel Core Ultra X7 Processor 358H", "Intel Core Ultra X7 358H"),
    ("AMD Ryzen AI 9 HX 370 Processor", "AMD Ryzen AI 9 HX 370"),
    ("Snapdragon X X1 26 100 Processor", "Snapdragon X X1-26-100"),
    ("Snapdragon X2 Elite X2E88100", "Snapdragon X2 Elite (18-core) X2E88100"),
    ("Intel Core i5-13450HX Processor Processor", "13th Gen Intel Core i5-13450HX"),
])
def test_one_cpu_in_two_spellings_has_one_key(left, right):
    assert cpu_part(left) == cpu_part(right) is not None


@pytest.mark.parametrize("left, right", [
    ("Intel Core i7-13620H", "Intel Core i7-13620HX"),
    ("AMD Ryzen AI 9 HX 370", "AMD Ryzen AI 9 370"),
    ("Intel Core Ultra 9 290HX Plus", "Intel Core Ultra 9 290HX"),
    ("AMD Ryzen 7 170", "AMD Ryzen AI 7 170"),
    ("AMD Ryzen AI MAX+ 395", "AMD Ryzen AI 9 395"),
    ("Snapdragon X X1 26 100", "Snapdragon X X1 26 101"),
])
def test_two_cpus_have_two_keys(left, right):
    assert cpu_part(left) != cpu_part(right)


def test_a_cpu_string_with_no_part_number_has_no_key():
    assert cpu_part("AMD HAWK POIN R7 6C") is None  # printed on ROG p84
    assert cpu_part("Unknown") is None
    assert cpu_part(None) is None
