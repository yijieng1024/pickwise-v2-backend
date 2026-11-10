"""The guide preview's warnings, on the production rows that showed each one was
needed (Q426 ASUS Commercial guide, 2026-11-09). Warnings never change a match."""
from datetime import datetime

from ._adapters import price_guide

CatalogRow, GuideRow, PricePoint = price_guide.CatalogRow, price_guide.GuideRow, price_guide.PricePoint
row_warnings = price_guide.row_warnings

FAMILY = "expertbook-b3-16"


def laptop(id, price=None, cpu="Intel Core Ultra 5 225H", ram=16, ssd=512, gpu="Intel Graphics",
           panel="Anti-glare display", family=FAMILY, name="ASUS ExpertBook"):
    return CatalogRow(id=id, model_code=f"asus-{id}", product_name=name, processor_model=cpu,
                      ram_gb=ram, ssd_gb=ssd, panel=panel, family_id=family, gpu=gpu, price_rm=price)


def guide(sku, price, cpu="Intel Core Ultra 5 Processor 225H", ram=16, ssd=512, panel=None):
    return GuideRow(sku=sku, model_code=sku.split("-")[0], price_rm=price, cpu=cpu, ram_gb=ram, ssd_gb=ssd, panel=panel)


def at(day, price, source=None, time="14:56"):
    return PricePoint(datetime.fromisoformat(f"{day} {time}"), price, source)


def found(warnings, kind, level="warning"):
    return next((w["message"] for w in warnings if (w["type"], w["level"]) == (kind, level)), None)


# --- possible duplicate row: B3605CCA U5 225H 16/512, 714137b2 and a4dd1eaf ---

def test_two_rows_of_one_family_with_the_same_configuration_are_a_possible_duplicate():
    kept, other = laptop("714137b2", 5999.0), laptop("a4dd1eaf", 8399.0)

    message = found(row_warnings(kept, [], [kept, other], []), "duplicate")

    assert "asus-a4dd1eaf" in message and "RM 8,399" in message


def test_a_suspended_duplicate_is_not_counted_because_the_caller_leaves_it_out():
    kept = laptop("714137b2", 5999.0)

    assert found(row_warnings(kept, [], [kept], []), "duplicate") is None


def test_one_chassis_with_two_gpus_is_not_a_duplicate():
    # Strix G16: without the GPU this fired 28 times on the ROG guide.
    rtx5060 = laptop("169349c2", 13999.0, gpu="NVIDIA GeForce RTX 5060 Laptop GPU")
    rtx5070 = laptop("f72d8838", 15999.0, gpu="NVIDIA GeForce RTX 5070 Laptop GPU")

    assert row_warnings(rtx5060, [], [rtx5060, rtx5070], []) == []


def test_rows_without_a_family_or_in_another_family_are_not_duplicates():
    alone, twin = laptop("a", family=None), laptop("b", family=None)
    elsewhere = laptop("c", family="expertbook-b5-16")

    assert row_warnings(alone, [], [alone, twin], []) == []
    assert row_warnings(laptop("d"), [], [elsewhere], []) == []


def test_two_rows_the_guide_prices_under_two_model_codes_are_two_machines():
    # 0fa49d4c (B3605CCA) and 416b293a (B3606CCA): same family and configuration, both RM 7,199.
    b3605, b3606 = laptop("0fa49d4c", 7199.0), laptop("416b293a", 7199.0)
    rows = {"0fa49d4c": [guide("B3605CCA-MB2197X", 7199.0)], "416b293a": [guide("B3606CCA-MB7150X", 7199.0)]}

    assert row_warnings(b3605, rows["0fa49d4c"], [b3605, b3606], [], rows) == []
    # With only one of them in the guide, nothing says they are two machines.
    assert found(row_warnings(b3605, rows["0fa49d4c"], [b3605, b3606], [], {}), "duplicate")
    assert found(row_warnings(b3606, [], [b3605, b3606], [], rows), "duplicate")


B3605 = 'ASUS ExpertBook B3 (B3605) (B3605CCA) (Intel Core Ultra 7 255H, 16GB RAM, 512GB SSD)'
B3606 = 'ASUS ExpertBook B3 G1 (16" Intel) (B3606CCA) (Intel Core Ultra 7 255H, 16GB RAM, 512GB SSD)'
BM1403 = "ASUS ExpertBook BM1 (BM1403) (AMD Ryzen 5 150, 16GB RAM, 512GB SSD)"


def test_two_base_model_codes_are_two_machines_with_no_guide_row_at_all():
    # The "Not in this guide" tab: 0fa49d4c and 416b293a kept warning in the other guides.
    b3605, b3606 = laptop("0fa49d4c", 7199.0, name=B3605), laptop("416b293a", 7199.0, name=B3606)

    assert row_warnings(b3605, [], [b3605, b3606], []) == []
    assert row_warnings(b3606, [], [b3605, b3606], []) == []


def test_the_same_base_model_code_is_still_a_possible_duplicate():
    # 714137b2 / a4dd1eaf are both B3605CCA; 38246257 / 4bccb37c are both BM1403.
    kept, other = laptop("714137b2", 5999.0, name=B3605), laptop("a4dd1eaf", 8399.0, name=B3605)
    bm_a, bm_b = laptop("38246257", 5999.0, name=BM1403), laptop("4bccb37c", 4298.0, name=BM1403)

    assert found(row_warnings(kept, [], [kept, other], []), "duplicate")
    assert found(row_warnings(bm_a, [], [bm_a, bm_b], []), "duplicate")
    assert found(row_warnings(bm_b, [], [bm_a, bm_b], []), "duplicate")


def test_a_name_with_no_model_code_exempts_nothing():
    named, bare = laptop("a", name=B3605), laptop("b", name='ASUS ExpertBook B3 G2 (16" Intel)')

    assert found(row_warnings(named, [], [named, bare], []), "duplicate")


def test_the_second_half_of_a_sku_in_a_name_is_not_a_model_code():
    # Two ROG rows of one model, each named with its own SKU: G615 is shared, PS5104 and MS5078 are not codes.
    one = laptop("a", name="ROG Strix G16 (2025) G615 (G615L-PS5104W)")
    two = laptop("b", name="ROG Strix G16 (2025) G615 (G615L-MS5078W)")

    assert price_guide.base_codes(one) == {"G615"}
    assert found(row_warnings(one, [], [one, two], []), "duplicate")


# --- suspected wrong price: BM1403 b6edc91d, stored 4,299, guide 5,799 ---

def test_a_guide_price_far_from_a_stored_price_that_a_sibling_shares_is_suspected():
    bm1403 = laptop("b6edc91d", 4299.0, cpu="AMD Ryzen 7 170")
    sibling = laptop("sibling", 4299.0, cpu="AMD Ryzen 5 150")

    message = found(row_warnings(bm1403, [guide("BM1403CDA-S60711X", 5799.0)], [bm1403, sibling], []), "wrong_price")

    assert "RM 5,799 is +35% from the stored RM 4,299" in message
    assert "asus-sibling" in message and "copied" in message


def test_a_price_the_guide_confirms_is_not_a_copied_price():
    # Six Consumer Q3 rows share a price with a sibling and the guide prints that price.
    row, sibling = laptop("a", 3999.0), laptop("b", 3999.0, ram=8)

    assert row_warnings(row, [guide("S3607V-ARP181WS", 3999.0)], [row, sibling], []) == []


def test_the_price_gap_starts_at_ten_percent():
    row = laptop("a", 5000.0)

    assert found(row_warnings(row, [guide("X-1W", 5500.0)], [row], []), "wrong_price")
    assert found(row_warnings(row, [guide("X-1W", 4500.0)], [row], []), "wrong_price")
    assert row_warnings(row, [guide("X-1W", 5499.0)], [row], []) == []


def test_a_laptop_with_no_stored_price_has_no_price_to_suspect():
    row = laptop("a", 0.0)

    assert row_warnings(row, [guide("X-1W", 5799.0)], [row, laptop("b", 0.0, ram=8)], []) == []


# --- multiple SKUs that may not match: 41113426, B9406CAA U7 358H 32GB/1TB ---

B9406 = [
    guide("B9406CAA-TH1139X", 16499.0), guide("B9406CAA-TH1140WS", 11999.0),
    guide("B9406CAA-TH1142WS", 11999.0), guide("B9406CAA-TH1446X", 16499.0),
]


def test_skus_with_identical_printed_specs_but_two_prices_and_two_suffixes_are_flagged():
    row = laptop("41113426", 11999.0)

    warnings = row_warnings(row, B9406, [row], [])

    message = found(warnings, "multi_sku")
    assert "differ in price, SKU suffix:" in message
    assert "B9406CAA-TH1139X (RM 16,499)" in message and "B9406CAA-TH1140WS (RM 11,999)" in message
    # The wrong-price check reads the guide price furthest from the stored one.
    assert "RM 16,499 is +38%" in found(warnings, "wrong_price")


def test_two_skus_with_the_same_specs_price_and_suffix_are_fine():
    row = laptop("6cb4d6db", 4999.0)

    assert row_warnings(row, [guide("UX3405C-AQL246WS", 4999.0), guide("UX3405C-AQL248WS", 4999.0)], [row], []) == []


def test_two_model_codes_on_one_laptop_are_flagged():
    row = laptop("e808cf68", 3799.0)

    message = found(row_warnings(row, [guide("P3606CCA-MB5150WS", 3799.0), guide("P3606CCAP-MB5150WS", 3799.0)], [row], []), "multi_sku")

    assert "differ in model code:" in message


def test_w_and_wp_are_one_suffix_as_they_are_one_sku():
    row = laptop("a", 5999.0)

    assert row_warnings(row, [guide("GU606A-RTB128W", 5999.0), guide("GU606A-RTB129WP", 5999.0)], [row], []) == []


# --- price history problem: P5606CCA e2d4f792 ---

def test_the_history_p5606cca_had_before_the_clean_up_is_a_warning():
    row = laptop("e2d4f792", 7999.0)
    history = [
        at("2026-08-14", 5299.0, time="14:56"), at("2026-08-14", 0.0, time="14:57"),
        at("2026-08-15", 9899.0, time="08:31"), at("2026-11-09", 7999.0, "guide", time="07:10"),
    ]

    message = found(row_warnings(row, [], [row], history), "price_history")

    assert "An entry of RM 0 on 2026-08-14" in message
    assert "RM 5,299 then RM 0 within 24 hours" in message
    assert "RM 0 then RM 9,899 within 24 hours" in message


def test_a_jump_written_by_a_guide_is_info_and_not_a_warning():
    # What P5606CCA's history is today: 5,299 from a page, 7,999 from the guide.
    row = laptop("e2d4f792", 7999.0)
    history = [at("2026-08-14", 5299.0), at("2026-11-09", 7999.0, "guide")]

    warnings = row_warnings(row, [], [row], history)

    assert found(warnings, "price_history") is None
    assert "RM 5,299 to RM 7,999 (+51%) on 2026-11-09" in found(warnings, "price_history", level="info")


def test_a_jump_not_written_by_a_guide_is_a_warning():
    row = laptop("125001f2", 7499.0)
    history = [at("2026-06-29", 4999.0), at("2026-11-01", 7499.0, "page")]

    warnings = row_warnings(row, [], [row], history)

    assert "RM 4,999 to RM 7,499 (+50%)" in found(warnings, "price_history")
    assert found(warnings, "price_history", level="info") is None


def test_a_change_under_thirty_percent_a_day_apart_is_quiet():
    row = laptop("a", 6000.0)

    assert row_warnings(row, [], [row], [at("2026-08-14", 5000.0), at("2026-08-16", 6000.0)]) == []


def test_a_latest_entry_that_is_not_the_stored_price_is_a_warning():
    row = laptop("a", 6000.0)

    message = found(row_warnings(row, [], [row], [at("2026-08-14", 5900.0)]), "price_history")

    assert message == "Latest entry RM 5,900 is not the stored price RM 6,000"
