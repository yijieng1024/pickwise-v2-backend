"""GET /admin/pickscore/summary."""
from app.laptops.laptop_models import LaptopPickScore, LaptopStatus
from app.laptops.pickscore_general import USE_CASE_PRIORITIES
from tests.integration.conftest import make_laptop

SUMMARY = "/api/v2/admin/pickscore/summary"


def test_summary_counts_problems_distribution_and_top(admin_client, session, brand):
    ok = make_laptop(brand.id, product_name="Good One")
    unverified = make_laptop(brand.id, product_name="Odd CPU", processor_model="Snapdragon X2 Plus X2P-64-100")
    withheld = make_laptop(brand.id, product_name="Nothing Known", processor_model="Unknown", gpu_model="Intel Graphics")
    retired = make_laptop(brand.id, status=LaptopStatus.SUSPENDED.value)
    session.add_all([ok, unverified, withheld, retired])
    session.commit()
    for uc in USE_CASE_PRIORITIES:
        session.add_all([
            LaptopPickScore(laptop_id=ok.id, use_case=uc, score=95, flags={}),
            LaptopPickScore(laptop_id=unverified.id, use_case=uc, score=42, flags={"cpu_benchmark_unresolved": True}),
            LaptopPickScore(laptop_id=withheld.id, use_case=uc, score=None, flags={
                "score_withheld": True, "cpu_benchmark_unresolved": True, "gpu_benchmark_unresolved": True}),
            LaptopPickScore(laptop_id=retired.id, use_case=uc, score=10, flags={}),
        ])
    session.commit()

    body = admin_client.get(SUMMARY).json()

    s = body["status"]
    assert (s["active"], s["scored"], s["withheld"], s["unverified"], s["missing"]) == (3, 2, 1, 2, 0)

    # Withheld first, then by name; diagnosis only for the unresolved side.
    items = body["problems"]["items"]
    assert body["problems"]["total"] == 2
    assert [i["product_name"] for i in items] == ["Nothing Known", "Odd CPU"]
    assert items[0]["cpu"]["diagnosis"]["kind"] == "placeholder"
    assert items[0]["gpu"]["diagnosis"]["kind"] == "no_igpu_entry"
    assert items[1]["cpu"]["diagnosis"]["kind"] == "qualcomm_part_missing"
    assert items[1]["gpu"]["diagnosis"] is None

    gaming = body["distribution"]["by_use_case"]["gaming"]
    assert gaming["counts"][9] == 1 and gaming["counts"][4] == 1 and gaming["withheld"] == 1
    assert sum(gaming["counts"]) == 2  # the suspended laptop is not counted

    assert [r["product_name"] for r in body["top"]["general_use"]] == ["Good One", "Odd CPU"]
    assert body["weights"]["gaming"]["gpu"]["weight"] == USE_CASE_PRIORITIES["gaming"]["gpu"]
