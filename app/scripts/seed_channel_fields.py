"""
Seed youtube_channels.review_language from the videos we have already ingested.

Migration d1f21e5d2152 split trust_tier into evidence_tier / market_relevance /
review_language. evidence_tier was backfilled inside the migration (it is a
lossless copy of trust_tier). The other two cannot be derived from trust_tier
-- that is the whole point of the split -- so they landed on their server
defaults, `global` and `en`.

`review_language` can be seeded from data rather than guessed: every ingested
video carries its title, and the script a channel titles in is a direct
statement of the language it reviews in. This classifies each channel by the
titles attributed to it:

    all CJK titles      -> zh
    all ASCII titles    -> en
    both                -> mixed
    no videos ingested  -> left alone (no evidence either way)

The signal is unusually clean on the current roster -- 10 of the 11 channels
with videos are unanimous, and the one that is not is genuinely bilingual --
but it is still derived, so this script is DRY RUN BY DEFAULT and prints the
full per-channel assignment for review. Pass --apply to write.

`market_relevance` is deliberately NOT seeded. Nothing in the database says
which currency a reviewer quotes; a channel's language does not imply its
market (the entire reason the two are separate fields, and see the note below).
It is a human judgement on 12 rows. This script only reports the current value
so the gap is visible.

Usage:
    python -m app.scripts.seed_channel_fields              # dry run
    python -m app.scripts.seed_channel_fields --apply
    python -m app.scripts.seed_channel_fields --apply --force   # re-seed rows
                                                                # already set

On Windows set PYTHONUTF8=1 or the CJK channel names print as mojibake.
"""
import argparse
import re
from collections import Counter, defaultdict

from sqlmodel import Session, select

from app.database import engine
from app.reviews.models import (
    RawYoutubeReview,
    ReviewLanguage,
    YoutubeChannel,
)

# CJK Unified Ideographs + Hiragana/Katakana + Hangul, the same class
# app/scripts/audit_match_ties.py uses. Shared deliberately: if the two ever
# disagree about what counts as a CJK title, the tie audit and the channel
# roster would be describing different corpora.
_CJK = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯]"
)


def _title_language(title: str) -> str:
    return "zh" if _CJK.search(title or "") else "en"


def _classify(counts: Counter) -> str:
    """One channel's title mix -> a ReviewLanguage value."""
    zh, en = counts["zh"], counts["en"]
    if zh and en:
        return ReviewLanguage.MIXED.value
    if zh:
        return ReviewLanguage.ZH.value
    return ReviewLanguage.EN.value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the seeded values. Without it the script only reports.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also overwrite channels whose review_language is already "
        "something other than the 'en' default — i.e. one a human has set. "
        "Off by default so a hand correction is never silently reverted.",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        channels = session.exec(select(YoutubeChannel)).all()
        reviews = session.exec(
            select(RawYoutubeReview.channel_id, RawYoutubeReview.video_title)
        ).all()

        per_channel: dict[str, Counter] = defaultdict(Counter)
        for channel_id, title in reviews:
            per_channel[channel_id][_title_language(title)] += 1

        print(f"channels: {len(channels)}   ingested videos: {len(reviews)}")
        print()
        header = (
            f"{'channel':<28} {'videos':>6} {'zh/en':>8}  "
            f"{'current':<7} {'seed':<7} {'market':<9} action"
        )
        print(header)
        print("-" * len(header))

        changes = 0
        skipped_no_data = 0
        skipped_set = 0

        for channel in sorted(channels, key=lambda c: c.channel_name):
            counts = per_channel.get(channel.channel_id, Counter())
            total = counts["zh"] + counts["en"]
            name = channel.channel_name[:27]

            if total == 0:
                # No evidence either way. Leaving the default in place is the
                # honest outcome: a channel with no ingested videos has not
                # told us anything, and writing 'en' would look like a finding.
                skipped_no_data += 1
                action = "skip (no videos)"
                seed = "-"
            else:
                seed = _classify(counts)
                already_set = channel.review_language != ReviewLanguage.EN.value
                if seed == channel.review_language:
                    action = "unchanged"
                elif already_set and not args.force:
                    skipped_set += 1
                    action = "skip (set by hand — use --force)"
                else:
                    action = f"SET {channel.review_language} -> {seed}"
                    changes += 1
                    if args.apply:
                        channel.review_language = seed
                        session.add(channel)

            print(
                f"{name:<28} {total:>6} {f'{counts["zh"]}/{counts["en"]}':>8}  "
                f"{channel.review_language:<7} {seed:<7} "
                f"{channel.market_relevance:<9} {action}"
            )

        if args.apply and changes:
            session.commit()

        print()
        print(f"would change      : {changes}" if not args.apply else f"changed           : {changes}")
        print(f"skipped, no videos: {skipped_no_data}")
        print(f"skipped, hand-set : {skipped_set}")
        if not args.apply:
            print()
            print("DRY RUN — nothing written. Re-run with --apply to commit.")

        _report_market_gap(channels)


def _report_market_gap(channels: list[YoutubeChannel]) -> None:
    """market_relevance is not seeded, so say plainly what it currently claims.

    This is the field that carries the coverage gap: language and market are
    independent, and a roster that is linguistically diverse can still be
    uniformly wrong on pricing.
    """
    counts = Counter(c.market_relevance for c in channels)
    print()
    print("market_relevance (NOT seeded — human judgement, 12 rows):")
    for value, n in counts.most_common():
        print(f"  {value:<10} {n}")
    if counts.get("my", 0) == 0:
        print()
        print(
            "  No channel is marked `my`. Every price figure entering the review\n"
            "  corpus today is quoted in a foreign currency against foreign\n"
            "  retail. Set this per channel before anything consumes it."
        )


if __name__ == "__main__":
    main()
