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


@pytest.mark.parametrize("printed, stored", [("GU606A-RTB128W", "GU606A-RTB128WP"), ("G835L-WGSA378WP", "G835L-WGSA378W")])
def test_w_and_wp_are_one_sku_but_the_match_waits_for_the_admin(printed, stored):
    result = match([guide(sku=printed, cpu=None, ram=None, ssd=None)], [catalog(sku=stored)])

    assert [c.id for c, _ in result.suffix_rule] == ["a"]
    assert result.matched == [] and result.uncovered == []


def test_one_guide_row_prices_both_the_w_and_the_wp_laptop():
    laptops = [catalog(id="w", sku="GU606A-MTB038W"), catalog(id="wp", sku="GU606A-MTB038WP")]

    result = match([guide(sku="GU606A-MTB038W", cpu=None, ram=None, ssd=None)], laptops)

    assert [c.id for c, _ in result.matched] == ["w"]  # the exact SKU needs no rule
    assert [c.id for c, _ in result.suffix_rule] == ["wp"]
    assert result.ambiguous == []


def test_an_exact_sku_is_matched_even_with_its_twin_at_the_same_price():
    rows = [guide(sku="GU606A-MTB038W"), guide(sku="GU606A-MTB038WP")]

    result = match(rows, [catalog(sku="GU606A-MTB038W")])

    assert [c.id for c, _ in result.matched] == ["a"]
    assert result.suffix_rule == []


def test_a_guide_that_prices_w_and_wp_apart_is_a_conflict():
    rows = [guide(sku="GU606A-MTB038W", price=16999.0), guide(sku="GU606A-MTB038WP", price=17999.0)]

    result = match(rows, [catalog(sku="GU606A-MTB038W")])

    assert result.matched == []
    assert len(result.conflict) == 1


@pytest.mark.parametrize("printed, stored", [("A1407C-ALY145WS", "A1407C-ALY145W"), ("UX3407Q-ASB555WSP", "UX3407Q-ASB555WS")])
def test_other_endings_stay_apart(printed, stored):
    assert match([guide(sku=printed, cpu=None, ram=None, ssd=None)], [catalog(sku=stored)]).matched == []


def test_model_code_and_specs_match():
    result = match([guide()], [catalog()])

    assert [c.id for c, _ in result.matched] == ["a"]
    assert result.unmatched_guide == [] and result.uncovered == []


def test_a_match_through_the_base_code_waits_for_an_admin():
    laptop = catalog(model_code="asus-expertbook-p1403", name="ASUS ExpertBook P1 (P1403)")

    result = match([guide()], [laptop])

    assert result.matched == []
    assert [c.id for c, _ in result.base_code] == ["a"]
    assert result.uncovered == []


def test_another_letter_on_the_catalog_side_is_still_only_a_base_code_match():
    laptop = catalog(model_code="asus-vivobook-s-14-s5406", name="ASUS Vivobook S 14 (S5406M)")

    result = match([guide(sku="S5406S-ALSF322WSP")], [laptop])

    assert result.matched == []
    assert [c.id for c, _ in result.base_code] == ["a"]


def test_a_laptop_naming_one_of_its_rows_full_codes_is_a_direct_match():
    laptop = catalog(name="ASUS Vivobook S16 (S3607AA)", model_code="asus-vivobook-s16-s3607")
    rows = [guide(sku="S3607AA-SH185WS"), guide(sku="S3607AB-ASH031WSP")]  # the laptop names only the first

    result = match(rows, [laptop])

    assert [c.id for c, _ in result.matched] == ["a"]
    assert result.base_code == []


def test_the_base_code_does_not_match_inside_another_code():
    laptop = catalog(model_code="asus-gp14035", name="ASUS GP14035")

    assert match([guide()], [laptop]).matched == []


def test_a_laptop_with_its_own_sku_cannot_take_another_skus_price():
    result = match([guide()], [catalog(sku="P1403CVA-OTHERX")])

    assert result.matched == []
    assert len(result.unmatched_guide) == 1


def test_a_laptop_with_its_own_sku_does_not_make_a_row_ambiguous():
    result = match([guide()], [catalog(id="a", sku="P1403CVA-OTHERX"), catalog(id="b")])

    assert [c.id for c, _ in result.matched] == ["b"]
    assert result.ambiguous == []


def test_the_letter_after_the_hyphen_picks_the_configuration():
    tuf = catalog(model_code="asus-tuf-gaming-f16", name="ASUS TUF Gaming F16 (2025) (FX608JM)", cpu="Intel Core i5-13450HX")
    rtx_5060 = guide(sku="FX608J-MRV040W", price=5799.0, cpu="Intel Core i5-13450HX")
    rtx_5050 = guide(sku="FX608J-HRV054W", price=5299.0, cpu="Intel Core i5-13450HX")

    result = match([rtx_5060, rtx_5050], [tuf])

    assert [[r.sku for r in rows] for _, rows in result.matched] == [["FX608J-MRV040W"]]
    assert [r.sku for r in result.unmatched_guide] == ["FX608J-HRV054W"]
    assert result.conflict == []


def test_a_catalog_row_with_nothing_after_the_code_is_not_checked():
    assert len(match([guide(sku="FX608J-MRV040W")], [catalog(name="ASUS TUF FX608J")]).matched) == 1


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
