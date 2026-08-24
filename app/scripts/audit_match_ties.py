"""
How often does the title matcher actually have a winner, and how wide is the tie?

matcher.py picks with `process.extractOne(..., scorer=fuzz.token_set_ratio)`,
which returns the FIRST maximum. token_set_ratio compares the two strings as
sets of whitespace-delimited tokens, so it is blind to anything the two match
keys do not differ on -- and sibling configurations of one machine differ only
in CPU/GPU/RAM tokens that a video title rarely contains. When several keys
tie at the top, `extractOne` does not pick the best one; it picks whichever
row the database happened to return first. That is not a ranking, it is row
order.

TIE WIDTH IS A MEASUREMENT, NOT A CONSTANT. `process.extract(limit=N)` returns
at most N rows, so any tie that is N-wide in the output may be wider in
reality. An earlier run of this script used limit=5 and reported "41 videos
are 5-wide" -- that was the truncation showing, not the data.

`--limit` therefore defaults to the FULL candidate count: the report cannot
truncate unless you ask it to. A fixed cap is the same bug deferred -- 40 is
comfortably above today's widest real tie (23), but that is a property of
today's 274 catalog rows, not an invariant, and the cap would start silently
clipping again as the catalog grows with nothing to announce it. The "N videos
hit the cap" line is still printed either way, so a deliberately lowered
--limit is honest about what it cost.

Reports, for two candidate scopes side by side (all laptops vs
status='active' only, which quantifies what the Phase 2 status filter buys):

  * tie-width distribution: 2 / 3 / 4 / 5 / 6-10 / 11-14 / 15+ tied at rank 1
  * per width bucket, same-family vs cross-family. A wide tie inside one
    family is config ambiguity (recoverable -- any member is roughly the right
    machine); a wide tie spanning families is matcher failure.
  * the rank-1 vs rank-2 gap distribution
  * the same statistics split by CJK vs ASCII-only titles, because
    token_set_ratio tokenizes on whitespace and Chinese has none: a CJK title
    arrives as one enormous token, so the set intersection is driven entirely
    by whatever Latin spans happen to be space-separated in it

Read-only, no network: it re-uses matcher._build_match_key so the keys are
byte-identical to production's, SELECTs the catalog and the review rows, and
writes nothing.

Usage:
    python -m app.scripts.audit_match_ties
    python -m app.scripts.audit_match_ties --limit 20   # deliberately truncated
    python -m app.scripts.audit_match_ties --status pending
    python -m app.scripts.audit_match_ties --show 25
"""
import argparse
import re
from collections import Counter, defaultdict

from rapidfuzz import fuzz, process
from sqlalchemy import select as sa_select
from sqlmodel import Session, select

from app.database import engine

# Mapper registration for a standalone script (same chain the other audit
# scripts import): Laptop -> LaptopCustomization -> Category are linked by
# string relationship names that SQLAlchemy can only resolve once the classes
# have been imported.
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.reviews.matcher import MATCH_THRESHOLD, _build_match_key
from app.reviews.models import RawYoutubeReview

# CJK Unified Ideographs + Hiragana/Katakana + Hangul. Presence of any of
# these means whitespace tokenization no longer describes the title.
_CJK = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯]"
)

# Ordered so the report reads left-to-right from "easy human decision" to
# "unreviewable". Boundaries chosen to match how a review queue would
# escalate: a 2-way call is a glance, 6-10 is a page of options, 15+ is not a
# decision a human can make from a title alone.
_WIDTH_BUCKETS = [
    ("1 (clear winner)", lambda w: w == 1),
    ("2", lambda w: w == 2),
    ("3", lambda w: w == 3),
    ("4", lambda w: w == 4),
    ("5", lambda w: w == 5),
    ("6-10", lambda w: 6 <= w <= 10),
    ("11-14", lambda w: 11 <= w <= 14),
    ("15+", lambda w: w >= 15),
]


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _load_candidates(session: Session, active_only: bool) -> list[dict]:
    """Same join matcher.match_laptop() does, plus family_id so ties can be
    classified as within-family or cross-family."""
    stmt = sa_select(Laptop, LaptopBrand.name).join(
        LaptopBrand, LaptopBrand.id == Laptop.brand_id
    )
    if active_only:
        stmt = stmt.where(Laptop.status == LaptopStatus.ACTIVE.value)
    rows = session.execute(stmt).all()
    return [
        {
            "key": _build_match_key(brand_name, laptop.product_name),
            "laptop_id": laptop.id,
            "family_id": laptop.family_id,
            "product_name": laptop.product_name,
            "brand": brand_name,
            "status": laptop.status,
        }
        for laptop, brand_name in rows
    ]


def _family_label(cand: dict) -> str:
    """A null family_id is 'not grouped yet', which is NOT the same as being in
    a family with the other tied row -- treat each null as its own group so
    ungrouped ties are never miscounted as recoverable config ambiguity."""
    if cand["family_id"]:
        return str(cand["family_id"])
    return f"unassigned:{cand['laptop_id']}"


def _bucket_gap(gap: float) -> str:
    if gap == 0:
        return "0 (true tie)"
    if gap <= 1:
        return "<=1"
    if gap <= 2:
        return "<=2"
    if gap <= 5:
        return "<=5"
    return ">5"


def _width_bucket(width: int) -> str:
    for label, test in _WIDTH_BUCKETS:
        if test(width):
            return label
    return "15+"


def _analyse(title: str, candidates: list[dict], keys: list[str], limit: int) -> dict:
    scored = process.extract(title, keys, scorer=fuzz.token_set_ratio, limit=limit)
    if not scored:
        return {}

    top = [
        {"score": float(score), "cand": candidates[idx]} for _key, score, idx in scored
    ]
    best = top[0]["score"]
    gap = best - top[1]["score"] if len(top) > 1 else float(best)

    # Everything sharing the best score is a candidate the matcher could just
    # as legitimately have returned.
    tied = [t for t in top if t["score"] == best]
    families = {_family_label(t["cand"]) for t in tied}

    return {
        "top": top,
        "best": best,
        "gap": gap,
        "tie_width": len(tied),
        # The tie filled the whole result window, so its real width is >= this.
        # Reported separately rather than folded silently into the distribution.
        "capped": len(tied) >= limit,
        "tie_families": len(families),
        "above_threshold": best >= MATCH_THRESHOLD,
    }


def _report(label: str, entries: list[dict], limit: int) -> None:
    n = len(entries)
    print(f"--- {label} (n={n}) ---")
    if not n:
        print("  (none)")
        print()
        return

    capped = [e for e in entries if e["capped"]]
    print(
        f"  tie width (candidate limit={limit}; "
        f"{len(capped)} video(s) hit the cap"
        + (" -- WIDTHS BELOW ARE A FLOOR" if capped else "")
        + ")"
    )
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_bucket[_width_bucket(e["tie_width"])].append(e)

    print(f"    {'width':<18} {'videos':>7} {'':>8}  {'same-fam':>9} {'cross-fam':>10}")
    for bucket_label, _ in _WIDTH_BUCKETS:
        rows = by_bucket.get(bucket_label, [])
        if not rows:
            continue
        same = sum(1 for e in rows if e["tie_families"] == 1)
        cross = len(rows) - same
        print(
            f"    {bucket_label:<18} {len(rows):>7} "
            f"({len(rows) / n * 100:5.1f}%)  {same:>9} {cross:>10}"
        )

    ties = [e for e in entries if e["tie_width"] > 1]
    if ties:
        same = sum(1 for e in ties if e["tie_families"] == 1)
        cross = len(ties) - same
        widths = sorted(e["tie_width"] for e in ties)
        print(
            f"  tied at rank 1: {len(ties)} ({len(ties) / n * 100:.1f}%)  "
            f"width min {widths[0]} median {widths[len(widths) // 2]} max {widths[-1]}"
        )
        print(
            f"    within one family  {same:>5}  ({same / len(ties) * 100:5.1f}%)"
            "  config ambiguity, recoverable"
        )
        print(
            f"    across families    {cross:>5}  ({cross / len(ties) * 100:5.1f}%)"
            "  matcher failure, wrong machine can win"
        )
        above = sum(1 for e in ties if e["above_threshold"])
        print(
            f"    tied AND above MATCH_THRESHOLD={MATCH_THRESHOLD}: {above}"
            "  <- auto-matched to an arbitrary row today"
        )

    gaps = Counter(_bucket_gap(e["gap"]) for e in entries)
    print("  rank1-rank2 gap:")
    for bucket in ("0 (true tie)", "<=1", "<=2", "<=5", ">5"):
        c = gaps[bucket]
        print(f"    gap {bucket:<13} {c:>5}  ({c / n * 100:5.1f}%)")

    scores = sorted(e["best"] for e in entries)
    mid = scores[n // 2]
    print(f"  top-1 score: min {scores[0]:.1f}  median {mid:.1f}  max {scores[-1]:.1f}")
    print(
        f"  above MATCH_THRESHOLD={MATCH_THRESHOLD}: "
        f"{sum(1 for e in entries if e['above_threshold'])}/{n}"
    )
    print()


def _build_entries(reviews, candidates: list[dict], limit: int) -> list[dict]:
    keys = [c["key"] for c in candidates]
    entries = []
    for r in reviews:
        res = _analyse(r.video_title, candidates, keys, limit)
        if not res:
            continue
        res["video_id"] = r.video_id
        res["title"] = r.video_title
        res["status"] = r.status
        res["cjk"] = _has_cjk(r.video_title)
        entries.append(res)
    return entries


def _side_by_side(all_entries: list[dict], active_entries: list[dict], limit: int) -> None:
    """The whole point of the status filter is whether it narrows ties. Put the
    two distributions in one table so the delta is readable without arithmetic."""
    print("=== Tie-width distribution: ALL candidates vs ACTIVE-only ===")
    print(
        f"    {'width':<18} {'all':>7} {'same/cross':>12}   "
        f"{'active':>7} {'same/cross':>12}"
    )
    a_by: dict[str, list[dict]] = defaultdict(list)
    b_by: dict[str, list[dict]] = defaultdict(list)
    for e in all_entries:
        a_by[_width_bucket(e["tie_width"])].append(e)
    for e in active_entries:
        b_by[_width_bucket(e["tie_width"])].append(e)

    for bucket_label, _ in _WIDTH_BUCKETS:
        a = a_by.get(bucket_label, [])
        b = b_by.get(bucket_label, [])
        if not a and not b:
            continue
        a_same = sum(1 for e in a if e["tie_families"] == 1)
        b_same = sum(1 for e in b if e["tie_families"] == 1)
        print(
            f"    {bucket_label:<18} {len(a):>7} "
            f"{f'{a_same}/{len(a) - a_same}':>12}   "
            f"{len(b):>7} {f'{b_same}/{len(b) - b_same}':>12}"
        )

    for name, entries in (("all", all_entries), ("active-only", active_entries)):
        capped = sum(1 for e in entries if e["capped"])
        ties = [e for e in entries if e["tie_width"] > 1]
        cross = sum(1 for e in ties if e["tie_families"] > 1)
        widths = sorted(e["tie_width"] for e in ties) or [0]
        print(
            f"  {name:<12} ties {len(ties)}/{len(entries)}  "
            f"cross-family {cross}  max width {widths[-1]}  "
            f"hit cap({limit}) {capped}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", default=None, help="Only audit rows with this status (default: all)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Candidates retrieved per video. Ties can only be measured up to "
        "this width. Default 0 = every candidate, which cannot truncate; set a "
        "number only to trade accuracy for speed, and read the cap line.",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="Print the candidate list for the N widest ties.",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        all_candidates = _load_candidates(session, active_only=False)
        active_candidates = _load_candidates(session, active_only=True)
        stmt = select(RawYoutubeReview).order_by(
            RawYoutubeReview.created_at, RawYoutubeReview.video_id
        )
        if args.status:
            stmt = stmt.where(RawYoutubeReview.status == args.status)
        reviews = session.exec(stmt).all()

    status_note = f" (status={args.status})" if args.status else " (all statuses)"
    dup_all = sum(
        c - 1 for c in Counter(c["key"] for c in all_candidates).values() if c > 1
    )
    dup_active = sum(
        c - 1 for c in Counter(c["key"] for c in active_candidates).values() if c > 1
    )

    # 0 means "no cap": use the larger of the two scopes so both are measured
    # against the same window and the side-by-side columns stay comparable.
    limit = args.limit or max(len(all_candidates), len(active_candidates))

    print("=== Match-tie audit ===")
    print(
        f"candidate limit     : {limit}"
        + ("  (full candidate set — cannot truncate)" if not args.limit else "")
    )
    print(
        f"candidates (all)    : {len(all_candidates)} laptops"
        f"  ({dup_all} share a match key with another row)"
    )
    print(
        f"candidates (active) : {len(active_candidates)} laptops"
        f"  ({dup_active} share a match key with another row)"
    )
    print(f"catalog by status   : {dict(Counter(c['status'] for c in all_candidates))}")
    print(f"reviews audited     : {len(reviews)}{status_note}")
    print()

    if not all_candidates or not reviews:
        print("Nothing to audit.")
        return

    all_entries = _build_entries(reviews, all_candidates, limit)
    active_entries = _build_entries(reviews, active_candidates, limit)

    _side_by_side(all_entries, active_entries, limit)

    print("############ SCOPE: ALL CANDIDATES (what production does today) ##")
    _report("ALL TITLES", all_entries, limit)
    _report("ASCII-ONLY TITLES", [e for e in all_entries if not e["cjk"]], limit)
    _report("CJK TITLES", [e for e in all_entries if e["cjk"]], limit)

    print("############ SCOPE: ACTIVE CANDIDATES ONLY (Phase 2 item 8) ######")
    _report("ALL TITLES", active_entries, limit)
    _report("ASCII-ONLY TITLES", [e for e in active_entries if not e["cjk"]], limit)
    _report("CJK TITLES", [e for e in active_entries if e["cjk"]], limit)

    by_status = Counter(e["status"] for e in all_entries)
    print("--- reviews by stored status ---")
    for s, c in by_status.most_common():
        print(f"  {s:<10} {c}")
    print()

    worst = sorted(all_entries, key=lambda e: -e["tie_width"])[: args.show]
    if worst:
        print(f"--- {len(worst)} widest ties (all-candidate scope) ---")
        for e in worst:
            flag = "CJK" if e["cjk"] else "   "
            cap = "  [HIT CAP]" if e["capped"] else ""
            print(
                f"\n{flag} [{e['status']}] score={e['best']:.1f} gap={e['gap']:.1f} "
                f"tie_width={e['tie_width']} families={e['tie_families']}{cap}"
            )
            print(f"    title: {e['title'][:100]}")
            for t in e["top"][:8]:
                c = t["cand"]
                fam = str(c["family_id"])[:8] if c["family_id"] else "UNASSIGNED"
                print(f"      {t['score']:6.1f}  fam={fam:<10} {c['key'][:70]}")


if __name__ == "__main__":
    main()
