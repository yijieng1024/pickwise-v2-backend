"""The processor keeps the code the raw row states, not one the LLM invented."""
from ._adapters import processor_engine

check = processor_engine.check_model_code

RAW_NAME = "ASUS Vivobook S 14 (S5406) (S5406SAL)"
SPECS = '{"Processor": "Intel® Core™ Ultra 5 Processor 226V", "Graphics": "NVIDIA® GeForce RTX™ 5070"}'


def test_an_invented_code_becomes_the_raw_code_and_is_reported():
    name, invented = check("ASUS Vivobook S 14 (S5406M) (Intel Core Ultra 5 226V)", RAW_NAME, None, SPECS)
    assert name == "ASUS Vivobook S 14 (S5406SAL) (Intel Core Ultra 5 226V)"
    assert invented == ["S5406M"]


def test_a_correct_code_is_left_untouched():
    for good in ("ASUS Vivobook S 14 (S5406SAL) (16GB)", "ASUS Vivobook S 14 (S5406) (16GB)"):
        assert check(good, RAW_NAME, None, SPECS) == (good, [])


def test_a_raw_row_with_no_code_changes_nothing():
    hp = "HP EliteBook 8 G2i 14 inch Notebook"
    assert check("HP EliteBook 8 (X1234)", hp, None, "") == ("HP EliteBook 8 (X1234)", [])
    assert check("HP EliteBook 8", "", None, "") == ("HP EliteBook 8", [])


def test_the_sku_counts_as_raw():
    good = "ROG Strix G16 (G615L-MS5068W)"
    assert check(good, "ROG Strix G16 (2025) G615", "G615L-MS5068W", "") == (good, [])
    # With no code in the raw name, the SKU is the replacement.
    assert check("Zenbook (UX9999)", "Zenbook", "UX3405C-AQL246WS", "") == (
        "Zenbook (UX3405C-AQL246WS)", ["UX9999"],
    )


def test_a_part_named_in_the_specs_is_not_a_model_code():
    good = "ASUS Vivobook S 14 (S5406SAL) (RTX5070, 16GB)"
    assert check(good, RAW_NAME, None, SPECS) == (good, [])


def test_a_prefix_of_a_longer_raw_code_is_not_that_code():
    assert check("ASUS Vivobook S 14 (S5406SA)", RAW_NAME, None, SPECS) == (
        "ASUS Vivobook S 14 (S5406SAL)", ["S5406SA"],
    )


def test_every_copy_of_the_invented_code_is_replaced():
    assert check("Vivobook S5406M (S5406M)", RAW_NAME, None, SPECS) == (
        "Vivobook S5406SAL (S5406SAL)", ["S5406M"],
    )


def test_acer_and_rog_codes_are_read_whole():
    raw = "Acer Aspire 7 A715-59G-54Q6"
    assert check("Aspire 7 (A715-59G-54Q6)", raw, None, "") == ("Aspire 7 (A715-59G-54Q6)", [])
    assert check("Aspire 7 (A715-59G-99XX)", raw, None, "")[0] == "Aspire 7 (A715-59G-54Q6)"
