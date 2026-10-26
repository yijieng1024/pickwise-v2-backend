"""The admin Pick Score summary's pure parts (app/admin/pickscore_router.py).
The endpoint itself is in tests/integration/test_admin_pickscore.py."""
from tests.unit._adapters import SCORE_BUCKETS as BUCKETS, bucket_counts, use_case_priorities, weight_shares

USE_CASE_PRIORITIES = use_case_priorities()


def test_ten_point_buckets_with_100_in_the_top_one():
    counts = bucket_counts([0, 9, 10, 49, 50, 89, 90, 100])
    assert len(counts) == len(BUCKETS) == 10
    assert counts == [2, 1, 0, 0, 1, 1, 0, 0, 1, 2]


def test_shares_are_weight_over_total_and_sum_to_one():
    shares = weight_shares(USE_CASE_PRIORITIES)
    assert set(shares) == set(USE_CASE_PRIORITIES)
    for use_case, factors in shares.items():
        total = sum(USE_CASE_PRIORITIES[use_case].values())
        for f, v in factors.items():
            assert v["weight"] == USE_CASE_PRIORITIES[use_case][f]
            assert v["share"] == round(v["weight"] / total, 3)
        assert abs(sum(v["share"] for v in factors.values()) - 1) < 0.01
