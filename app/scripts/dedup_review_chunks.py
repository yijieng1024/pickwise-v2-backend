"""
Remove duplicate review chunks: keep one row per (video, window start, text).

11 of the 15 processed videos were processed twice before /process/{id} had a
duplicate guard, leaving 205 surplus rows (audit_review_foundation, section 1).
The duplicates skew everything downstream that counts chunks: the aggregator's
top-5 strengths/weaknesses and the per-laptop review summaries.

The key is (video_id, timestamp_start_seconds, chunk_text) as specified, plus
laptop_id: two rows for the same window attached to two different laptops are
not duplicates of each other, and none exist today (the dry run reports it if
that ever changes). Within a group the EARLIEST row (created_at, then id) is
kept -- the original run, which the summaries were first built from.

DRY RUN BY DEFAULT: read-only transaction, prints what it would delete.
`--apply` deletes and commits in one transaction. Targets DATABASE_URL.

After --apply, the affected laptops' summaries are stale: re-run
POST /reviews/aggregate/{laptop_id} for each laptop it lists.

Usage:
    python -m app.scripts.dedup_review_chunks            # dry run
    python -m app.scripts.dedup_review_chunks --apply
"""
import argparse
from collections import Counter, defaultdict

from sqlalchemy import delete, text
from sqlmodel import Session, select

from app.database import get_engine
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401
from app.laptops.laptop_models import Laptop
from app.reviews.models import LaptopReviewChunk


def find_surplus(rows) -> list:
    """Rows to delete: every row after the earliest in its duplicate group.
    `rows` are (id, video_id, laptop_id, start, text, created_at) tuples."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row[1], row[2], row[3], row[4])].append(row)
    surplus = []
    for members in groups.values():
        members.sort(key=lambda r: (r[5], str(r[0])))
        surplus.extend(members[1:])
    return surplus


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = parser.parse_args()

    with Session(get_engine()) as session:
        if not args.apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        C = LaptopReviewChunk
        rows = session.exec(select(
            C.id, C.video_id, C.laptop_id, C.timestamp_start_seconds, C.chunk_text, C.created_at,
        )).all()
        surplus = find_surplus(rows)

        same_window_other_laptop = sum(
            1 for n in Counter((r[1], r[3], r[4]) for r in rows).values() if n > 1
        ) - sum(1 for n in Counter((r[1], r[2], r[3], r[4]) for r in rows).values() if n > 1)
        per_video = Counter(r[1] for r in surplus)
        laptops = sorted({r[2] for r in surplus}, key=str)
        names = dict(session.exec(
            select(Laptop.id, Laptop.product_name).where(Laptop.id.in_(laptops))  # type: ignore[attr-defined]
        ).all()) if laptops else {}

        print(f"chunk rows: {len(rows)}")
        print(f"{'WILL DELETE' if args.apply else 'WOULD DELETE'}: {len(surplus)} rows "
              f"across {len(per_video)} videos -> {len(rows) - len(surplus)} rows left")
        print(f"groups differing only by laptop_id (kept, not duplicates): {same_window_other_laptop}")
        for vid, n in per_video.most_common():
            print(f"    {vid:<12} {n:>3} rows")
        print("summaries to re-aggregate afterwards (POST /reviews/aggregate/{laptop_id}):")
        for lid in laptops:
            print(f"    {lid}  {names.get(lid, '(deleted laptop)')}")
        if surplus:
            sample = surplus[0]
            print(f"sample row deleted: id={sample[0]} video={sample[1]} start={sample[3]} "
                  f"text={sample[4][:70]!r}")

        if args.apply:
            ids = [r[0] for r in surplus]
            for i in range(0, len(ids), 500):
                session.exec(delete(C).where(C.id.in_(ids[i:i + 500])))  # type: ignore[attr-defined]
            session.commit()
            print(f"deleted {len(ids)} rows")
        else:
            session.rollback()
            print("dry run: nothing written (pass --apply to delete)")


if __name__ == "__main__":
    main()
