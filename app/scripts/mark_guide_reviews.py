"""
Mark two guide rows reviewed that Jack decided before the "reviewed" mark existed
(2026-11-15). One-off, after migration c7a3e9d2f4b6. Goes through the same service call
as the admin's "Mark reviewed", so a laptop no longer on a list of that guide stops the
script and writes nothing.

    python -m app.scripts.mark_guide_reviews            # prints the waiting counts, writes nothing
    python -m app.scripts.mark_guide_reviews --apply

  b6edc91d  ExpertBook BM1 BM1403 (Ryzen 7 170): used_guide, business guide 1f1b5ffb, RM 5,799
            (written with "Use guide price" before this mark existed)
  c769bd97  ROG Zephyrus G16 GU605C-PQR045WP: kept at RM 11,299, ROG guide price RM 14,999
            (retailers list RM 11,299; none near RM 14,999)

Reads and writes TEST_DATABASE_URL unless GUIDE_REVIEW_TARGET=production is set, and
checks that the URL is the database it claims to be (app/common/db_target.py). Safe to
run again: a second mark replaces the first.
"""
import app.all_models  # noqa: F401  every table, so a flush can resolve every foreign key
import argparse
import sys
import uuid

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlmodel import Session, select

from app.common.db_target import host_of, write_target
from app.laptops.laptop_models import PriceGuideDecision
from app.laptops.price_guide_model import PriceGuide
from app.laptops.price_guide_service import mark_reviewed, preview

MARKS = [
    (uuid.UUID("b6edc91d-c2f8-425a-b0d9-d30a8cc22746"), uuid.UUID("1f1b5ffb-58ab-4c64-8278-12f969975fba"),
     PriceGuideDecision.USED_GUIDE),
    (uuid.UUID("c769bd97-ae04-447f-87c1-29c07f93380a"), uuid.UUID("af4af544-0587-4ec1-940e-8d463d20cc2b"),
     PriceGuideDecision.KEPT),
]


def waiting(session: Session) -> dict[str, int]:
    """Per guide kind, what the weekly email calls "awaiting your confirmation"."""
    out = {}
    for guide in session.exec(select(PriceGuide).order_by(PriceGuide.kind)).all():
        counts = preview(session, guide)["counts"]
        out[guide.kind] = counts["matched_by_suffix_rule"] + counts["matched_by_base_code"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    args = parser.parse_args()

    load_dotenv()
    url = write_target("GUIDE_REVIEW_TARGET")
    print(f"{'WRITING TO' if args.apply else 'dry run, reading'} {host_of(url)}")
    engine = create_engine(url)
    with Session(engine) as session:
        before = waiting(session)
        print(f"waiting before: {before} = {sum(before.values())}")
        if args.apply:
            for laptop_id, guide_id, decision in MARKS:
                print(mark_reviewed(session, session.get(PriceGuide, guide_id), {laptop_id}, decision=decision))
            after = waiting(session)
            print(f"waiting after:  {after} = {sum(after.values())}")
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
