"""
Decode HTML entities in stored YouTube video titles (Review B3).

search.list returns titles HTML-escaped (`&amp;`, `&#39;`, `&quot;`, ...) and
they were stored as-is. Discovery now decodes them on the way in
(app/reviews/discovery.py); this applies the same `html.unescape` to the rows
stored before that. Every entity is decoded, not a fixed list.

Only `video_title` changes. match_confidence was scored on the escaped title
and is left as it is; POST /reviews/rematch re-scores pending rows if wanted.

DRY RUN BY DEFAULT: read-only transaction, prints every title it would change.
`--apply` updates and commits in one transaction. Targets DATABASE_URL.

Usage:
    python -m app.scripts.decode_review_titles            # dry run
    python -m app.scripts.decode_review_titles --apply
"""
import argparse
import html

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import get_engine
from app.reviews.models import RawYoutubeReview


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="update (default is a dry run)")
    args = parser.parse_args()

    with Session(get_engine()) as session:
        if not args.apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        rows = session.exec(
            select(RawYoutubeReview).order_by(RawYoutubeReview.video_title, RawYoutubeReview.id)
        ).all()
        changed = [(r, html.unescape(r.video_title)) for r in rows
                   if r.video_title and html.unescape(r.video_title) != r.video_title]
        for r, new in changed:
            print(f"{r.video_id:<12} {r.status:<10}\n    old: {r.video_title}\n    new: {new}")
        print(f"\n{len(changed)} of {len(rows)} titles would change.")

        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply.")
            return
        for r, new in changed:
            r.video_title = new
            session.add(r)
        session.commit()
        print(f"APPLIED: {len(changed)} titles decoded.")


if __name__ == "__main__":
    main()
