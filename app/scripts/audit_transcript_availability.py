"""
What is actually in the `rejected` bucket of raw_youtube_reviews?

transcript.py calls `ytt.fetch(video_id)` with no `languages` argument, and
YouTubeTranscriptApi.fetch() defaults to ('en',). A video whose only caption
track is Chinese therefore raises NoTranscriptFound and is recorded exactly
like a video with no captions at all, like a proxy failure, and like a deleted
video -- every failure path collapses to `return None`. This script re-opens
each rejected video with `.list()`, which enumerates the caption tracks
without committing to a language, and sorts the bucket into three unambiguous
groups:

    NO_TRACK        -- .list() succeeded, zero caption tracks exist.
                       A genuine gap. Only these need ASR.
    NON_ENGLISH     -- tracks exist but none is an 'en*' track. Recoverable
                       today by passing a language preference list.
    OTHER_FAILURE   -- .list() itself raised: proxy exhausted, IP blocked,
                       private/deleted video, network. Grouped by exception
                       class, because these are four different operational
                       problems wearing one label.

(A fourth group, HAS_ENGLISH, should be empty by construction -- a rejected
row with an English track means something other than language went wrong at
ingest time, most likely a transient proxy error, so it is reported too.)

Read-only: it opens a session, SELECTs, and never writes. It does make one
network call per video, so use --sample to bound proxy spend.

Usage:
    python -m app.scripts.audit_transcript_availability
    python -m app.scripts.audit_transcript_availability --sample 40
    python -m app.scripts.audit_transcript_availability --status pending --sample 10
"""
import argparse
import time
from collections import Counter

from sqlmodel import Session, select

from app.database import engine
from app.reviews.models import RawYoutubeReview
from app.reviews.transcript import _build_api

# Bucket names, kept as constants so the summary and the per-video lines can
# never disagree about spelling.
NO_TRACK = "NO_TRACK"
NON_ENGLISH = "NON_ENGLISH"
HAS_ENGLISH = "HAS_ENGLISH"
OTHER_FAILURE = "OTHER_FAILURE"


def _is_english(language_code: str) -> bool:
    """'en', 'en-US', 'en-GB', 'en-orig' all count. Codes are lowercased by
    YouTube already, but normalise anyway."""
    return language_code.lower().split("-")[0] == "en"


# youtube-transcript-api never returns an empty TranscriptList -- when a video
# has no captions at all it raises instead. TranscriptsDisabled is therefore
# the real "genuine gap" signal and belongs in NO_TRACK, not in the
# operational-failure bucket: no language preference and no proxy fix will
# ever produce text for these, so they are exactly the ASR decision set.
_NO_TRACK_EXCEPTIONS = {"TranscriptsDisabled", "NoTranscriptFound"}

# Same class-name test transcript.py::fetch_transcript uses to classify
# IP_BLOCKED, duplicated here rather than imported because that function
# classifies a fetch and this one classifies a .list(). Kept textually
# identical so the audit and the pipeline agree on what a block looks like.
def _is_ip_block(exception_name: str) -> bool:
    return "Blocked" in exception_name or "TooManyRequests" in exception_name


def _inspect(api, video_id: str) -> dict:
    """Enumerate caption tracks for one video. Never raises."""
    try:
        transcript_list = api.list(video_id)
    except Exception as e:
        name = type(e).__name__
        return {
            "bucket": NO_TRACK if name in _NO_TRACK_EXCEPTIONS else OTHER_FAILURE,
            "exception": name,
            "detail": str(e).strip().splitlines()[0][:160] if str(e).strip() else "",
            "tracks": [],
        }

    tracks = [
        {
            "language_code": t.language_code,
            "language": t.language,
            "generated": t.is_generated,
            "translatable": t.is_translatable,
        }
        for t in transcript_list
    ]

    if not tracks:
        bucket = NO_TRACK
    elif any(_is_english(t["language_code"]) for t in tracks):
        bucket = HAS_ENGLISH
    else:
        bucket = NON_ENGLISH

    return {"bucket": bucket, "exception": None, "detail": "", "tracks": tracks}


def _print_corpus_breakdown(session) -> None:
    """Whole-table counts, straight from the DB, no network. Printed first so a
    run that later aborts on a rate limit still yields the denominators the
    ADRs need."""
    rows = session.exec(
        select(RawYoutubeReview.status, RawYoutubeReview.failure_reason)
    ).all()
    total = len(rows)
    print("=== raw_youtube_reviews corpus ===")
    print(f"total rows : {total}")
    by_status = Counter(status for status, _ in rows)
    for status, n in by_status.most_common():
        print(f"  {status:<10} {n:>5}  ({n / (total or 1) * 100:5.1f}%)")

    print()
    print("failure_reason within status='rejected':")
    rejected = [fr for status, fr in rows if status == "rejected"]
    if not rejected:
        print("  (no rejected rows)")
    else:
        for reason, n in Counter(
            fr if fr is not None else "NULL (predates the column)"
            for fr in rejected
        ).most_common():
            print(f"  {reason:<28} {n:>5}  ({n / len(rejected) * 100:5.1f}%)")

    print()
    print("failure_reason across ALL statuses:")
    for reason, n in Counter(
        fr if fr is not None else "NULL (predates the column)" for _s, fr in rows
    ).most_common():
        print(f"  {reason:<28} {n:>5}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        default="rejected",
        help="raw_youtube_reviews.status to audit (default: rejected)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="Only inspect the first N rows — each row costs one proxy request.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Seconds between .list() calls. A residential IP hits YouTube's "
        "transcript rate limit at roughly 30 requests, and retrying inside the "
        "block window extends it, so this is a real cost control (default: 2.0).",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=3,
        help="Abort after this many CONSECUTIVE ip-block results. Once YouTube "
        "is blocking, every further call is both useless and self-harming — the "
        "remaining rows would all be miscounted as OTHER_FAILURE (default: 3).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Summary only, no per-video lines.",
    )
    args = parser.parse_args()

    with Session(engine) as session:
        _print_corpus_breakdown(session)
        rows = session.exec(
            select(RawYoutubeReview)
            .where(RawYoutubeReview.status == args.status)
            # Deterministic ordering so --sample means the same rows each run
            # and two runs are comparable.
            .order_by(RawYoutubeReview.created_at, RawYoutubeReview.video_id)
        ).all()

    total_in_status = len(rows)
    if args.sample:
        rows = rows[: args.sample]

    print(f"=== Transcript availability audit: status='{args.status}' ===")
    print(f"rows with this status : {total_in_status}")
    print(f"rows inspected        : {len(rows)}")
    print()

    api = _build_api()
    buckets: Counter = Counter()
    exceptions: Counter = Counter()
    lang_codes: Counter = Counter()
    generated_only = 0
    manual_available = 0

    consecutive_blocks = 0
    aborted_at = None

    for i, row in enumerate(rows, 1):
        if i > 1 and args.delay:
            time.sleep(args.delay)
        res = _inspect(api, row.video_id)

        # A run that pushes through a block produces numbers that look like
        # data but are really the block, so stop and say so instead.
        if res["exception"] and _is_ip_block(res["exception"]):
            consecutive_blocks += 1
        else:
            consecutive_blocks = 0

        buckets[res["bucket"]] += 1
        if res["exception"]:
            exceptions[res["exception"]] += 1
        for t in res["tracks"]:
            lang_codes[t["language_code"]] += 1
        if res["tracks"]:
            if any(not t["generated"] for t in res["tracks"]):
                manual_available += 1
            else:
                generated_only += 1

        if not args.quiet:
            title = (row.video_title or "")[:60]
            print(f"[{i}/{len(rows)}] {row.video_id}  {res['bucket']:<14} {title}")
            if res["exception"]:
                print(f"        !! {res['exception']}: {res['detail']}")
            for t in res["tracks"]:
                kind = "auto" if t["generated"] else "manual"
                xl = "translatable" if t["translatable"] else "not-translatable"
                print(f"        - {t['language_code']:<8} {kind:<7} {xl}  ({t['language']})")

        if consecutive_blocks >= args.max_blocks:
            aborted_at = i
            print()
            print(
                f"!! ABORTED after {consecutive_blocks} consecutive ip-block "
                f"results ({i}/{len(rows)} rows inspected). The buckets below "
                "are incomplete — wait out the block before re-running."
            )
            break

    print()
    print("=== Summary ===")
    inspected = (aborted_at or len(rows)) or 1
    if aborted_at:
        print(f"(partial run: {aborted_at} of {len(rows)} rows)")
    for name in (NO_TRACK, NON_ENGLISH, HAS_ENGLISH, OTHER_FAILURE):
        n = buckets[name]
        print(f"{name:<14} {n:>5}  ({n / inspected * 100:5.1f}%)")

    print()
    print("NO_TRACK      -> genuine gap; only these would need ASR.")
    print("NON_ENGLISH   -> recoverable by passing a language preference list.")
    print("HAS_ENGLISH   -> rejected despite an English track; transient/proxy failure.")
    print("OTHER_FAILURE -> .list() raised; see breakdown below.")

    if exceptions:
        print()
        print("=== Exceptions raised by .list(), by class ===")
        print("    (TranscriptsDisabled/NoTranscriptFound counted as NO_TRACK above)")
        for name, n in exceptions.most_common():
            print(f"  {name:<32} {n}")

    if lang_codes:
        print()
        print("=== Caption tracks seen (language_code -> count of videos) ===")
        for code, n in lang_codes.most_common():
            print(f"  {code:<10} {n}")
        print()
        print(f"videos with >=1 manually-created track : {manual_available}")
        print(f"videos with auto-generated tracks only : {generated_only}")


if __name__ == "__main__":
    main()
