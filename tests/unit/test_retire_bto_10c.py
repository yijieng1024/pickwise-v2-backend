"""10c step (f) plan (app/scripts/retire_bto_10c.py). Pure: rows in, changes out."""
import pytest

from ._adapters import retire_bto_10c as r

FAM = "fam-16-lr"
CHIP = {"processor_model": "Apple M5 Pro (18-core)", "gpu_model": "20-core GPU", "cpu_cores": 18, "gpu_cores": 20}


def _id(prefix):
    return f"{prefix}-0000-0000-0000-000000000000"


def _rows(**status):
    rows = [{"id": _id(k), "product_name": f"laptop {k}", "status": status.get(k, v), "family_id": FAM,
             "has_part_numbers": False, **CHIP, "cpu_cores": 18 if k == r.REVIEWED else 15}
            for k, v in r.EXPECTED.items()]
    target = {"id": _id(r.SURVEYED_TARGET), "product_name": "standard M5 Pro", "status": "active",
              "family_id": FAM, "has_part_numbers": True, **CHIP}
    return rows + [target]


LINKS = [{"raw_review_id": f"rev{i}", "family_id": FAM, "laptop_id": _id(r.REVIEWED)} for i in range(2)]


def test_every_row_goes_to_suspended_and_the_reviews_move_to_the_same_chip():
    result = r.plan(_rows(), LINKS, 49)
    assert len(result["status"]) == 10
    assert {(e["before"], e["after"]) for e in result["status"]} == {("active", "suspended"), ("inactive", "suspended")}
    rp = result["repoint"]
    assert (rp["from"][:8], rp["to"][:8], rp["chunks"], rp["links"]) == (r.REVIEWED, r.SURVEYED_TARGET, 49, 2)


def test_a_wrong_current_status_refuses_the_whole_plan():
    with pytest.raises(r.Refused, match="f24b8f91: status is 'suspended', expected 'inactive'"):
        r.plan(_rows(f24b8f91="suspended"), LINKS, 49)


def test_a_missing_or_doubled_id_refuses():
    rows = [x for x in _rows() if not x["id"].startswith("e60560b4")]
    with pytest.raises(r.Refused, match="e60560b4: 0 rows match"):
        r.plan(rows, LINKS, 49)
    with pytest.raises(r.Refused, match="e05aa3e6: 2 rows match"):
        r.plan(_rows() + [{**_rows()[0], "id": "e05aa3e6-ffff-0000-0000-000000000000"}], LINKS, 49)


@pytest.mark.parametrize("field, value", [("cpu_cores", 16), ("gpu_cores", 16), ("gpu_model", "16-core GPU"),
                                          ("processor_model", "Apple M5 Pro (15-core)")])
def test_a_target_whose_chip_differs_in_anything_refuses(field, value):
    rows = _rows()
    rows[-1][field] = value
    with pytest.raises(r.Refused, match="0 active standard configs with the same chip"):
        r.plan(rows, LINKS, 49)


def test_the_target_must_be_an_active_standard_config_and_the_surveyed_one():
    rows = _rows()
    rows[-1]["has_part_numbers"] = False
    with pytest.raises(r.Refused, match="0 active standard configs"):
        r.plan(rows, LINKS, 49)
    rows = _rows()
    rows[-1]["id"] = _id("99999999")
    with pytest.raises(r.Refused, match="is 99999999, not f3ebcce1"):
        r.plan(rows, LINKS, 49)
    with pytest.raises(r.Refused, match="2 active standard configs"):
        r.plan(_rows() + [{**_rows()[-1], "id": _id("88888888")}], LINKS, 49)


def test_a_link_the_target_already_has_refuses():
    taken = LINKS + [{**LINKS[0], "laptop_id": _id(r.SURVEYED_TARGET)}]
    with pytest.raises(r.Refused, match="1 links already exist"):
        r.plan(_rows(), taken, 49)
