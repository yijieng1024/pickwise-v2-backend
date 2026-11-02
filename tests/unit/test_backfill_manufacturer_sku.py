"""Which laptops get a SKU from their name. `plan` decides everything, so no database."""
from types import SimpleNamespace

from tests.unit._adapters import backfill_plan as plan


def _laptop(name, sku=None, status="active", laptop_id="a"):
    return SimpleNamespace(id=laptop_id, product_name=name, manufacturer_sku=sku, status=status)


def _reasons(laptops):
    writes, skips = plan(laptops)
    assert not writes
    return [why for _, why in skips]


def test_a_sku_in_a_rog_or_tuf_name_is_written_as_printed():
    rog = _laptop("ROG Strix G16 (2025) G615 (G615L-MS5078W)", laptop_id="a")
    tuf = _laptop("ASUS TUF Gaming F16 (FX608J-MRV040W)", laptop_id="b")

    assert plan([rog, tuf])[0] == [(rog, "G615L-MS5078W"), (tuf, "FX608J-MRV040W")]


def test_other_lines_are_left_out_and_not_reported():
    assert plan([_laptop("ASUS Vivobook S16 (M3607G-AB123W)")]) == ([], [])


def test_a_laptop_with_a_sku_is_never_rewritten():
    assert _reasons([_laptop("ROG Strix (G615L-MS5078W)", sku="G615L-OTHER")]) == ["already has G615L-OTHER"]


def test_a_suspended_laptop_is_skipped():
    assert _reasons([_laptop("ROG Strix (G615L-MS5078W)", status="suspended")]) == ["suspended"]


def test_a_name_with_no_sku_is_skipped():
    assert _reasons([_laptop("ASUS TUF Gaming A16 (2025) (FA608UP)")]) == ["no SKU in the name"]


def test_a_name_with_two_skus_is_skipped():
    reasons = _reasons([_laptop("ROG Strix (G615L-MS5078W) (G615L-PS5104W)")])

    assert reasons == ["more than one SKU in the name: G615L-MS5078W, G615L-PS5104W"]


def test_both_gu605c_rows_are_held_back_but_other_gu605c_skus_are_not():
    held = _laptop("ROG Zephyrus G16 (GU605C-PQR045W)", laptop_id="a")
    other = _laptop("ROG Zephyrus G16 (GU605C-XQR083W)", laptop_id="c")

    writes, skips = plan([held, _laptop(held.product_name, laptop_id="b"), other])

    assert writes == [(other, "GU605C-XQR083W")]
    assert [why for _, why in skips] == ["GU605C-PQR045W: held back until -W/-WP is known"] * 2


def test_a_sku_two_laptops_name_is_skipped_on_both():
    first, second = _laptop("ROG A (G615L-MS5078W)", laptop_id="a"), _laptop("ROG B (G615L-MS5078W)", laptop_id="b")

    assert _reasons([first, second]) == ["G615L-MS5078W: 2 laptops name it"] * 2


def test_w_and_wp_on_two_live_laptops_count_as_one_sku():
    plain, office = _laptop("ROG A (G606A-XTB038W)", laptop_id="a"), _laptop("ROG B (G606A-XTB038WP)", laptop_id="b")

    assert len(_reasons([plain, office])) == 2


def test_once_one_of_two_is_suspended_the_other_can_be_filled():
    keep = _laptop("ROG A (G606A-XTB038WP)", laptop_id="a")
    retired = _laptop("ROG B (G606A-XTB038W)", status="suspended", laptop_id="b")

    assert plan([keep, retired]) == ([(keep, "G606A-XTB038WP")], [(retired, "suspended")])


def test_a_sku_another_laptop_holds_is_skipped_even_as_the_other_suffix():
    holder = _laptop("ASUS Laptop", sku="G615L-MS5078WP", laptop_id="b")

    assert _reasons([holder, _laptop("ROG Strix (G615L-MS5078W)")]) == ["G615L-MS5078W: another laptop holds it"]
