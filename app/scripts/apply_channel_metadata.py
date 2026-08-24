"""
Apply reviewed market_relevance / review_language values to youtube_channels.

This is the human half of the trust_tier split (migration d1f21e5d2152).
seed_channel_fields.py derives review_language from ingested video titles and
therefore cannot help a channel with zero ingested videos — which was every
Malaysian channel on the roster when they were registered. They all landed on
the server defaults, so the schema asserted `market_relevance = global` for
eight channels that are demonstrably Malaysian. This script carries the
decision that fixed that.

The assignments below are a RECORD OF A REVIEWED DECISION, not a heuristic.
Each was made from evidence gathered on 2026-08-24 by sampling each channel's
50 most recent upload titles plus its channel description via the YouTube Data
API (2 quota units per channel, cheap because playlistItems.list costs 1 unit
against search.list's 100):

  * market_relevance=my — every one states Malaysian operation in its own
    description ("from Kuala Lumpur, Malaysia", "Malaysia's Largest Online
    Community", "马来西亚发展最快的网络科技媒体") and quotes RM figures in titles.
  * review_language — from the script of those titles. KLGadgetTV is the one
    that title script alone could not settle: all 50 titles are English but
    the presenters are Chinese Malaysian, and titles are not speech. It was
    confirmed `en` by a human watching the channel.

evidence_tier values themselves are NOT written here — a human set them
directly after spot-checking each channel. What this script does write is
`--mark-reviewed`, stamping evidence_tier_reviewed_at on the channels below.
That column exists because "confirmed tier_2" and "nobody looked" are the same
byte in evidence_tier, and the aggregator now orders on that byte; NULL means
genuinely unreviewed. Stamping is separate from --apply so re-running the
metadata assignment never silently re-dates a review.

Idempotent and safe to re-run: it only writes cells that differ. Dry run by
default.

Usage:
    python -m app.scripts.apply_channel_metadata           # show the diff
    python -m app.scripts.apply_channel_metadata --apply

On Windows set PYTHONUTF8=1 or the CJK channel names print as mojibake.
"""
import argparse
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.database import engine
from app.reviews.models import (
    MARKET_RELEVANCE_VALUES,
    REVIEW_LANGUAGE_VALUES,
    YoutubeChannel,
)

# Keyed on channel_name because that is what a human reviewed against. A name
# that no longer resolves is reported, never silently skipped — YouTube display
# names can change, and a silent miss here would leave a channel asserting the
# wrong market with nothing to show for it.
ASSIGNMENTS: dict[str, dict[str, str]] = {
    "SoyaCincau":       {"market_relevance": "my", "review_language": "en"},
    "TechNave":         {"market_relevance": "my", "review_language": "en"},
    "Lowyat TV":        {"market_relevance": "my", "review_language": "en"},
    "Adam Lobo TV":     {"market_relevance": "my", "review_language": "en"},
    "KLGadgetTV":       {"market_relevance": "my", "review_language": "en"},
    "TechNave 中文版":    {"market_relevance": "my", "review_language": "zh"},
    "Zing Gadget":      {"market_relevance": "my", "review_language": "zh"},
    "可恩Ke En":         {"market_relevance": "my", "review_language": "zh"},
}

_ALLOWED = {
    "market_relevance": MARKET_RELEVANCE_VALUES,
    "review_language": REVIEW_LANGUAGE_VALUES,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes.")
    parser.add_argument(
        "--mark-reviewed",
        action="store_true",
        help="Also stamp evidence_tier_reviewed_at=now on the channels above, "
        "recording that a human judged their evidence_tier. Requires --apply. "
        "Only pass this when a review actually happened.",
    )
    args = parser.parse_args()

    # Validate the table itself before touching the database. SQLModel skips
    # validation on table=True models, so a typo here would otherwise be
    # written straight through the enum and only surface much later.
    for name, fields in ASSIGNMENTS.items():
        for field, value in fields.items():
            if value not in _ALLOWED[field]:
                raise SystemExit(
                    f"{name}: {field}={value!r} is not a valid value "
                    f"({sorted(_ALLOWED[field])})"
                )

    with Session(engine) as session:
        channels = {c.channel_name: c for c in session.exec(select(YoutubeChannel)).all()}

        missing = [n for n in ASSIGNMENTS if n not in channels]
        changes = 0
        unchanged = 0

        print(f"{'channel':<24} {'field':<18} {'current':<9} -> {'new':<9} action")
        print("-" * 72)
        for name, fields in ASSIGNMENTS.items():
            channel = channels.get(name)
            if channel is None:
                continue
            for field, value in fields.items():
                current = getattr(channel, field)
                if current == value:
                    unchanged += 1
                    print(f"{name[:23]:<24} {field:<18} {current:<9} -> {value:<9} unchanged")
                    continue
                changes += 1
                print(f"{name[:23]:<24} {field:<18} {current:<9} -> {value:<9} SET")
                if args.apply:
                    setattr(channel, field, value)
                    session.add(channel)

        stamped = 0
        if args.mark_reviewed:
            now = datetime.now(timezone.utc)
            print()
            print("--- evidence_tier review stamps ---")
            for name in ASSIGNMENTS:
                channel = channels.get(name)
                if channel is None:
                    continue
                previous = channel.evidence_tier_reviewed_at
                print(
                    f"{name[:23]:<24} evidence_tier={channel.evidence_tier:<7} "
                    f"reviewed_at {previous or 'NULL'} -> {now:%Y-%m-%d %H:%M}"
                )
                stamped += 1
                if args.apply:
                    channel.evidence_tier_reviewed_at = now
                    session.add(channel)

        if args.apply and (changes or stamped):
            session.commit()

        print()
        print(f"{'changed' if args.apply else 'would change':<14}: {changes}")
        print(f"{'already correct':<14}: {unchanged}")
        if args.mark_reviewed:
            print(f"{'review-stamped':<14}: {stamped}")
        if missing:
            print()
            print("!! NOT FOUND in youtube_channels (nothing written for these):")
            for name in missing:
                print(f"   - {name}")
        if not args.apply:
            print()
            print("DRY RUN — nothing written. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
