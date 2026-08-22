"""
How often does the title matcher actually have a winner?

matcher.py picks with `process.extractOne(..., scorer=fuzz.token_set_ratio)`,
which returns the FIRST maximum. token_set_ratio compares the two strings as
sets of whitespace-delimited tokens, so it is blind to anything the two match
keys do not differ on -- and sibling configurations of one machine differ only
in CPU/GPU/RAM tokens that a video title rarely contains. When several keys
tie at the top, `extractOne` does not pick the best one; it picks whichever
row the database happened to return first. That is not a ranking, it is row
order.

This script recomputes the top 5 candidates per video without deciding
anything, and reports:

  * the gap between rank 1 and rank 2 (0 == a true tie)
  * how many videos sit at gap 0 / <=1 / <=2 / <=5
  * for tied videos, whether the tied candidates are all in ONE laptop family
    (config ambiguity -- recoverable) or span SEVERAL families (a real matcher
    failure -- the wrong machine can win)
  * the same statistics split by CJK vs ASCII-only titles, because
    token_set_ratio tokenizes on whitespace and Chinese has none: a CJK title
    arrives as one enormous token, so the set intersection is driven entirely
    by whatever Latin spans happen to be space-separated in it

Read-only, no network: it re-uses matcher._build_match_key so the keys are
byte-identical to production's, SELECTs the catalog and the review rows, and
writes nothing.

Usage:
    python -m app.scripts.audit_match_ties
    python -m app.scripts.audit_match_ties --status pending
    python -m app.scripts.audit_match_ties --show 25
    python -m app.scripts.audit_match_ties --active-only
"""
import argparse
import re
from collections import Counter

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

_TOP_N = 5

# CJK Unified Ideographs + Hiragana/Katakana + Hangul. Presence of any of
# these means whitespace tokenization no longer describes the title.
_CJK = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯]"
)


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


def _analyse(title: str, candidates: list[dict], keys: list[str]) -> dict:
    scored = process.extract(title, keys, scorer=fuzz.token_set_ratio, limit=_TOP_N)
    if not scored:
        return {}

    top = [
        {"score": float(score), "cand": candidates[idx]} for _key, score, idx in scored
    ]
    best = top[0]["score"]
    gap = best - top[1]["score"] if len(top) > 1 else float(best)

    # Everything sharing the best score is a candidate the matcher could just
    # as legitimately have returned. Note limit=_TOP_N truncates this, so a tie
    # counted as 5-wide may be wider in reality.
    tied = [t for t in top if t["score"] == best]
    families = {_family_label(t["cand"]) for t in tied}

    return {
        "top": top,
        "best": best,
        "gap": gap,
        "tie_width": len(tied),
        "tie_families": len(families),
        "above_threshold": best >= MATCH_THRESHOLD,
    }


def _report(label: str, entries: list[dict]) -> None:
    n = len(entries)
    print(f"--- {label} (n={n}) ---")
    if not n:
        print("  (none)")
        print()
        return

    gaps = Counter(_bucket_gap(e["gap"]) for e in entries)
    for bucket in ("0 (true tie)", "<=1", "<=2", "<=5", ">5"):
        c = gaps[bucket]
        print(f"  gap {bucket:<13} {c:>5}  ({c / n * 100:5.1f}%)")

    cumulative = 0
    print("  cumulative:")
    for bucket in ("0 (true tie)", "<=1", "<=2", "<=5"):
        cumulative += gaps[bucket]
        print(f"    gap {bucket:<13} {cumulative:>5}  ({cumulative / n * 100:5.1f}%)")

    ties = [e for e in entries if e["gap"] == 0]
    if ties:
        same = sum(1 for e in ties if e["tie_families"] == 1)
        cross = len(ties) - same
        print(f"  ties: {len(ties)}")
        print(
            f"    within one family  {same:>5}  ({same / len(ties) * 100:5.1f}%)"
            "  config ambiguity, recoverable"
        )
        print(
            f"    across families    {cross:>5}  ({cross / len(ties) * 100:5.1f}%)"
            "  matcher failure, wrong machine can win"
        )
        widths = Counter(e["tie_width"] for e in ties)
        print("    tie width: " + ", ".join(f"{w}x{c}" for w, c in sorted(widths.items())))
        above = sum(1 for e in ties if e["above_threshold"])
        print(
            f"    tied AND above MATCH_THRESHOLD={MATCH_THRESHOLD}: {above}"
            "  <- auto-matched to an arbitrary row today"
        )

    scores = sorted(e["best"] for e in entries)
    mid = scores[n // 2]
    print(f"  top-1 score: min {scores[0]:.1f}  median {mid:.1f}  max {scores[-1]:.1f}")
    print(
        f"  above MATCH_THRESHOLD={MATCH_THRESHOLD}: "
        f"{sum(1 for e in entries if e['above_threshold'])}/{n}"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status", default=None, help="Only audit rows with this status (default: all)"
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Build candidates from active laptops only (production currently "
        "does NOT do this -- see Phase 2 item 8)",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=15,
        help="Print the top-5 candidate list for the N closest calls.",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        candidates = _load_candidates(session, args.active_only)
        stmt = select(RawYoutubeReview).order_by(
            RawYoutubeReview.created_at, RawYoutubeReview.video_id
        )
        if args.status:
            stmt = stmt.where(RawYoutubeReview.status == args.status)
        reviews = session.exec(stmt).all()

    keys = [c["key"] for c in candidates]
    dup_keys = sum(c - 1 for c in Counter(keys).values() if c > 1)
    scope = " (active only)" if args.active_only else " (all statuses -- as production does)"
    status_note = f" (status={args.status})" if args.status else " (all statuses)"

    print("=== Match-tie audit ===")
    print(f"candidates          : {len(candidates)} laptops{scope}")
    print(f"identical match keys: {dup_keys} rows share a key with another row")
    print(f"reviews audited     : {len(reviews)}{status_note}")
    print()

    if not candidates or not reviews:
        print("Nothing to audit.")
        return

    entries = []
    for r in reviews:
        res = _analyse(r.video_title, candidates, keys)
        if not res:
            continue
        res["video_id"] = r.video_id
        res["title"] = r.video_title
        res["status"] = r.status
        res["cjk"] = _has_cjk(r.video_title)
        entries.append(res)

    _report("ALL TITLES", entries)
    _report("ASCII-ONLY TITLES", [e for e in entries if not e["cjk"]])
    _report("CJK TITLES", [e for e in entries if e["cjk"]])

    by_status = Counter(e["status"] for e in entries)
    print("--- reviews by stored status ---")
    for s, c in by_status.most_common():
        print(f"  {s:<10} {c}")
    print()

    worst = sorted(entries, key=lambda e: (e["gap"], -e["tie_width"]))[: args.show]
    if worst:
        print(f"--- {len(worst)} closest calls (top-{_TOP_N} candidates each) ---")
        for e in worst:
            flag = "CJK" if e["cjk"] else "   "
            print(
                f"\n{flag} [{e['status']}] gap={e['gap']:.1f} "
                f"tie_width={e['tie_width']} families={e['tie_families']}"
            )
            print(f"    title: {e['title'][:100]}")
            for t in e["top"]:
                c = t["cand"]
                fam = str(c["family_id"])[:8] if c["family_id"] else "UNASSIGNED"
                print(f"      {t['score']:6.1f}  fam={fam:<10} {c['key'][:70]}")


if __name__ == "__main__":
    main()
