"""
Move PENDING reviews that already have a human link to MATCHED (Review B).

From B1 on, adding a human link moves a PENDING review to MATCHED (POST
/reviews/{id}/links and PATCH /reviews/raw/{id}/match). Links made before that
left their reviews PENDING; this applies the same rule to them once.

Selection is the rule itself: status PENDING and at least one human link.
Each row is printed with what B1's processor would do with it afterwards:
  processable  one family, at most one config -> /process-bulk picks it up
  queue        several families or configs   -> stays in GET /reviews/pending

DRY RUN BY DEFAULT: read-only transaction, prints what it would change.
`--apply` updates and commits in one transaction. Targets DATABASE_URL.

Usage:
    python -m app.scripts.promote_human_linked_reviews            # dry run
    python -m app.scripts.promote_human_linked_reviews --apply
"""
import argparse

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import get_engine
from app.laptops.family_model import LaptopFamily
from app.reviews.link_model import MatchSource, ReviewLaptopLink
from app.reviews.link_service import links_by_review, processing_target
from app.reviews.models import RawYoutubeReview, ReviewStatus
from app.reviews.processor import _chunk_transcript


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="update (default is a dry run)")
    args = parser.parse_args()

    with Session(get_engine()) as session:
        if not args.apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        human = select(ReviewLaptopLink.raw_review_id).where(
            ReviewLaptopLink.match_source == MatchSource.HUMAN.value
        )
        reviews = session.exec(
            select(RawYoutubeReview)
            .where(RawYoutubeReview.status == ReviewStatus.PENDING.value)
            .where(RawYoutubeReview.id.in_(human))  # type: ignore[attr-defined]
            .order_by(RawYoutubeReview.video_title, RawYoutubeReview.id)
        ).all()
        links = links_by_review(session, [r.id for r in reviews])
        names = dict(session.exec(select(LaptopFamily.id, LaptopFamily.name)).all())

        processable = windows = 0
        for r in reviews:
            target = processing_target(links[r.id])
            segs = (r.raw_transcript or {}).get("segments", [])
            n = len(_chunk_transcript(segs)) if segs else 0
            if target:
                processable += 1
                windows += n
                what = f"processable  {'config' if target[1] else 'family'}  {names.get(target[0], '?')[:26]}"
            else:
                fams = {l.family_id for l in links[r.id]}
                what = f"queue, not processable ({len(fams)} families)"
            print(f"{r.video_id:<12} pending -> matched  {what:<54} {n:>3} windows  {r.video_title[:50]}")

        print(f"\n{len(reviews)} reviews: {processable} processable ({windows} windows, "
              f"one Gemini call + one embedding each), {len(reviews) - processable} stay in "
              "GET /reviews/pending and are refused by /process-bulk until Review C")

        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply.")
            return
        for r in reviews:
            r.status = ReviewStatus.MATCHED.value
            session.add(r)
        session.commit()
        print(f"APPLIED: {len(reviews)} reviews set to matched.")


if __name__ == "__main__":
    main()
