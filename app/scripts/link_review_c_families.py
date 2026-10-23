"""
Add the human family-only links Review C needs before its accuracy check and
re-run (decided 2026-10-23):

    TEJdZf31x6s  MacBook Neo vs Air vs Pro    Neo + Air 13 + Air 15 + Pro 14 + Pro 16
    Nhl23S_4zk0  MacBook Neo / Air / Pro 怎麼選  Neo + Air 13 + Air 15 + Pro 14
    1X6Mtv_wZ1g  M5 MacBook Pro 實測           Pro 14 + Air 13 + Air 15

From the descriptions and transcripts: Nhl23S_4zk0 tests a MacBook Neo, an M4
and an M5 MacBook Air and an M5 MacBook Pro; 1X6Mtv_wZ1g an M5 MacBook Pro
against the reviewer's M4 MacBook Air. Neither states the Air's size, so both
Air families are linked (each window is assigned on its own). The base M5 chip
is only in the 14-inch Pro.

TEJdZf31x6s gets a human Neo link too: its only link is an AUTO link, and a
review's human links replace its auto links for every reader, so adding Air
and Pro links alone would drop the Neo.

Links go through link_service.create_human_link (match_source human,
match_confidence NULL, laptop_id NULL), a PENDING review moves to MATCHED as
on the link routes, and processor.on_links_changed marks an already processed
review for re-processing. --apply needs the Review C migration (b8e2d4f6a1c3).

DRY RUN BY DEFAULT: read-only transaction, prints the exact rows. `--apply`
inserts them in one transaction. Targets DATABASE_URL.

Usage:
    python -m app.scripts.link_review_c_families            # dry run
    python -m app.scripts.link_review_c_families --apply
"""
import argparse

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import get_engine
from app.laptops.family_model import LaptopFamily
from app.reviews.link_model import ReviewLaptopLink
from app.reviews.link_service import create_human_link
from app.reviews.models import RawYoutubeReview, ReviewStatus

PROPOSED = {
    "TEJdZf31x6s": ["MacBook Neo", "Apple MacBook Air 13", "Apple MacBook Air 15",
                    "Apple MacBook Pro 14", "Apple MacBook Pro 16"],
    "Nhl23S_4zk0": ["MacBook Neo", "Apple MacBook Air 13", "Apple MacBook Air 15", "Apple MacBook Pro 14"],
    "1X6Mtv_wZ1g": ["Apple MacBook Pro 14", "Apple MacBook Air 13", "Apple MacBook Air 15"],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="insert (default is a dry run)")
    args = parser.parse_args()

    with Session(get_engine()) as session:
        if not args.apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        families = {}
        for name in {n for names in PROPOSED.values() for n in names}:
            rows = session.exec(select(LaptopFamily.id).where(LaptopFamily.name == name)).all()
            if len(rows) != 1:
                raise SystemExit(f"STOP: family name {name!r} matches {len(rows)} families.")
            families[name] = rows[0]
        names = dict(session.exec(select(LaptopFamily.id, LaptopFamily.name)).all())

        planned = []
        for video_id, wanted in PROPOSED.items():
            review = session.exec(
                select(RawYoutubeReview.id, RawYoutubeReview.status, RawYoutubeReview.video_title)
                .where(RawYoutubeReview.video_id == video_id)
            ).one()
            existing = session.exec(
                select(ReviewLaptopLink).where(ReviewLaptopLink.raw_review_id == review.id)
            ).all()
            print(f"\n{video_id}  status={review.status}  {review.video_title[:70]}")
            for l in existing:
                print(f"  existing  {l.match_source:<5} {names.get(l.family_id)}  laptop_id={l.laptop_id}")
            for name in wanted:
                fid = families[name]
                if any(l.family_id == fid and l.laptop_id is None and l.match_source == "human"
                       for l in existing):
                    print(f"  skip      {name} (human family-only link exists)")
                    continue
                planned.append((review.id, video_id, fid, name))
                print(f"  INSERT    review_laptop_link(raw_review_id={review.id}, family_id={fid} "
                      f"[{name}], laptop_id=NULL, match_source=human, match_confidence=NULL)")
            if any(l.match_source == "auto" for l in existing):
                print("  note      its auto link(s) stop counting once it has a human link")
            if review.status == ReviewStatus.PENDING.value:
                print("  UPDATE    status pending -> matched")

        print(f"\n{len(planned)} links to insert.")
        if not args.apply:
            print("DRY RUN: nothing written. Re-run with --apply.")
            return

        from app.reviews.processor import on_links_changed

        for review_id, _, fid, _ in planned:
            create_human_link(session, review_id, fid, None)
        for review_id in {p[0] for p in planned}:
            review = session.get(RawYoutubeReview, review_id)
            if review.status == ReviewStatus.PENDING.value:
                review.status = ReviewStatus.MATCHED.value
                session.add(review)
            on_links_changed(session, review_id)
        session.commit()
        print(f"APPLIED: {len(planned)} links inserted.")


if __name__ == "__main__":
    main()
