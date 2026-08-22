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
        "--quiet",
        action="store_true",
        help="Summary only, no per-video lines.",
    )
    args = parser.parse_args()

    with Session(engine) as session:
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

    for i, row in enumerate(rows, 1):
        res = _inspect(api, row.video_id)
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

    print()
    print("=== Summary ===")
    inspected = len(rows) or 1
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
