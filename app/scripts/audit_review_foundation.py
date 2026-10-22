"""
Is the review pipeline's foundation clean enough to build on? (Review A, 2.1 plan step 2)

This is the DETECTOR. The fixes in Review A are the defence, and the recurring
lesson in this codebase (ADR-0012, Consequences) is that the two must be
separate: a fix that looks applied is only proven by an independent check that
does not share its code path. So this script re-derives every number from the
raw tables and never calls the guards it is auditing. It is meant to be re-run
after each phase.

Reports:

  1. Duplicate chunks. A chunk has no raw_review_id column, so "the review" is
     its video_id (raw_youtube_reviews.video_id is UNIQUE, so that is 1:1).
       a. exact duplicates: same (video_id, timestamp_start, chunk_text)
       b. window duplicates: same (video_id, timestamp_start), any text. A
          re-run paraphrases the same window again and the LLM need not produce
          byte-identical text, so (a) alone can undercount reprocessing. (b)
          is the upper bound; the gap between them is how much (a) missed.
  2. Partial reviews: any chunk exists (which is what /process-bulk treats as
     "processed") but fewer distinct windows were saved than the transcript
     yields. "Expected" re-uses processor._chunk_transcript so the window count
     is byte-identical to what process_raw_review would produce.
  3. (removed in A2 -- it compared matched_laptop_id with the links, and the
     column is gone. Reviews needing a human link are listed by section 7.)
     split into "no link at all" (the backfill set, expected 3) and "links
     exist, but none names this laptop" (the two sources disagree).
  4. Human links that matching would overwrite today:
       a. /rematch: it rewrites every PENDING row the matcher scores above
          threshold. A family-only human match leaves the row PENDING, so it is
          exposed. Re-runs match_laptop (local fuzzy match, no network, no
          writes) to say which rows would actually flip, not just which could.
       b. ingest: should_skip_existing re-processes REJECTED rows, rewriting
          status and auto links. A human link on a rejected row is exposed.
  5. Chunks whose laptop has no config-level link on their review — what a
     link deletion leaves behind (input to A1 step 4).
  6. Every reference to matched_laptop_id in the code (static scan).

READ-ONLY, enforced, not promised: the session runs SET TRANSACTION READ ONLY
before its first query, so an accidental write raises instead of landing.
No network apart from the database. Targets DATABASE_URL — production.

Usage:
    python -m app.scripts.audit_review_foundation
"""
import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import get_engine

# Mapper registration for a standalone script (same chain the other audit
# scripts import).
from app.laptops.customization_model import LaptopCustomization  # noqa: F401
from app.taxonomy.category_model import Category  # noqa: F401

from app.reviews.link_model import MatchSource, ReviewLaptopLink
from app.reviews.matcher import match_laptop
from app.reviews.models import LaptopReviewChunk, RawYoutubeReview, ReviewStatus
from app.reviews.processor import _chunk_transcript

_REPO = Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("app", "eval", "tests", "alembic")
_COLUMN = re.compile(r"\bmatched_laptop_id\b")


def _title(r: RawYoutubeReview, width: int = 60) -> str:
    t = r.video_title or ""
    return t if len(t) <= width else t[: width - 1] + "…"


def _heading(n: str, label: str) -> None:
    print(f"\n{'=' * 78}\n{n}. {label}\n{'=' * 78}")


def audit_duplicates(chunks, reviews_by_video) -> None:
    _heading("1", "Duplicate chunks")
    exact = Counter((c.video_id, c.timestamp_start_seconds, c.chunk_text) for c in chunks)
    window = Counter((c.video_id, c.timestamp_start_seconds) for c in chunks)

    exact_extra = sum(n - 1 for n in exact.values() if n > 1)
    window_extra = sum(n - 1 for n in window.values() if n > 1)
    print(f"chunk rows: {len(chunks)}")
    print(f"(a) exact duplicates  (video, start, text): {exact_extra} surplus rows "
          f"-> {len(chunks) - exact_extra} unique")
    print(f"(b) window duplicates (video, start):       {window_extra} surplus rows "
          f"-> {len(chunks) - window_extra} unique")
    if window_extra != exact_extra:
        print(f"    {window_extra - exact_extra} surplus rows re-paraphrased the same window "
              "with different text: (a) does not catch them.")

    per_video: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for c in chunks:
        per_video[c.video_id][0] += 1
    for (vid, _, _), n in exact.items():
        per_video[vid][1] += n - 1
    for (vid, _), n in window.items():
        per_video[vid][2] += n - 1

    affected = {v: t for v, t in per_video.items() if t[2]}
    print(f"\nvideos with chunks: {len(per_video)}; videos with window duplicates: {len(affected)}")
    print(f"  {'video_id':<12} {'rows':>5} {'exact+':>7} {'window+':>8}  "
          f"{'max copies':>10}  title")
    for vid, (rows, ex, wi) in sorted(per_video.items(), key=lambda kv: -kv[1][2]):
        copies = max(n for (v, _), n in window.items() if v == vid)
        r = reviews_by_video.get(vid)
        print(f"  {vid:<12} {rows:>5} {ex:>7} {wi:>8}  {copies:>10}  "
              f"{_title(r) if r else '(no raw review row!)'}")


def audit_partial(chunks, reviews_by_video) -> None:
    _heading("2", "Reviews treated as processed that are actually partial")
    saved_windows: dict[str, set[int]] = defaultdict(set)
    for c in chunks:
        saved_windows[c.video_id].add(c.timestamp_start_seconds)

    partial, complete, no_transcript = [], 0, []
    for vid, windows in saved_windows.items():
        r = reviews_by_video.get(vid)
        segments = (r.raw_transcript or {}).get("segments", []) if r else []
        if not segments:
            no_transcript.append(vid)
            continue
        expected = {w["start"] for w in _chunk_transcript(segments)}
        missing = expected - windows
        if missing:
            partial.append((r, len(windows & expected), len(expected), len(windows - expected)))
        else:
            complete += 1

    print(f"processed reviews (>=1 chunk): {len(saved_windows)}")
    print(f"  complete (every expected window saved): {complete}")
    print(f"  PARTIAL  (some windows missing):         {len(partial)}")
    print(f"  no transcript to compare against:        {len(no_transcript)}"
          + (f"  {no_transcript}" if no_transcript else ""))
    for r, got, exp, stray in sorted(partial, key=lambda p: p[1] / p[2]):
        extra = f", {stray} saved windows not in today's chunking" if stray else ""
        print(f"  {r.video_id:<12} {got:>3}/{exp:<3} windows  status={r.status:<10} "
              f"{_title(r, 50)}{extra}")


def audit_overwrite(session, reviews, links_by_review) -> None:
    _heading("4", "Human links that matching would overwrite today")
    human = {
        rid: ls for rid, ls in links_by_review.items()
        if any(l.match_source == MatchSource.HUMAN.value for l in ls)
    }
    by_id = {r.id: r for r in reviews}
    status_of = Counter(by_id[rid].status for rid in human if rid in by_id)
    print(f"reviews with >=1 human link: {len(human)}  by status: {dict(status_of)}")

    exposed_pending = [by_id[rid] for rid in human
                       if rid in by_id and by_id[rid].status == ReviewStatus.PENDING.value]
    would_flip = []
    for r in exposed_pending:
        laptop_id, score = match_laptop(r.video_title, session)
        if laptop_id:
            human_cfgs = {l.laptop_id for l in human[r.id]
                          if l.match_source == MatchSource.HUMAN.value}
            would_flip.append((r, laptop_id, score, laptop_id in human_cfgs))
    print(f"\n(a) /rematch: human-linked PENDING reviews: {len(exposed_pending)}; "
          f"matcher clears threshold today (WOULD be rewritten to MATCHED): {len(would_flip)}")
    for r, lid, score, agrees in would_flip:
        verdict = "same config as a human link" if agrees else "DISAGREES with the human links"
        print(f"    {r.video_id:<12} score={score:5.1f} -> {lid}  ({verdict})  {_title(r, 40)}")

    exposed_rejected = [by_id[rid] for rid in human
                        if rid in by_id and by_id[rid].status == ReviewStatus.REJECTED.value]
    print(f"\n(b) ingest: human-linked REJECTED reviews (re-processed, and "
          f"status/links rewritten, on the next ingest that discovers them): "
          f"{len(exposed_rejected)}")
    for r in exposed_rejected:
        print(f"    {r.video_id:<12} failure={r.failure_reason}  {_title(r, 50)}")


def audit_orphan_chunks(chunks, reviews_by_video, links_by_review) -> None:
    _heading("5", "Chunks whose laptop has no config-level link on their review")
    orphans: Counter = Counter()
    for c in chunks:
        r = reviews_by_video.get(c.video_id)
        cfgs = {l.laptop_id for l in links_by_review.get(r.id, [])} if r else set()
        if c.laptop_id not in cfgs:
            reason = "no raw review row" if r is None else f"status={r.status}"
            orphans[(c.video_id, reason)] += 1
    print(f"chunk rows not backed by a link (review, laptop_id): {sum(orphans.values())}")
    for (vid, reason), n in orphans.most_common():
        print(f"    {vid:<12} {n:>4} rows  {reason}")


def audit_link_rules(reviews, chunks, links_by_review) -> None:
    """Section 7: what the link-only rules (Review A) do to production data.
    Re-derived here, NOT imported from link_service/processor: a detector that
    calls the code it audits can only ever agree with it."""
    _heading("7", "Link-only rules: processable reviews and the human queue")

    def config(review_id):
        ls = links_by_review.get(review_id, [])
        human = [l for l in ls if l.match_source == MatchSource.HUMAN.value]
        cfgs = {l.laptop_id for l in (human or ls) if l.laptop_id}
        return cfgs.pop() if len(cfgs) == 1 else None

    saved = defaultdict(set)
    for c in chunks:
        saved[(c.video_id, c.laptop_id)].add(c.timestamp_start_seconds)

    matched = [r for r in reviews if r.status == ReviewStatus.MATCHED.value]
    processable, candidates, queue_matched = [], [], []
    for r in matched:
        lid = config(r.id)
        if lid is None:
            queue_matched.append(r)
            continue
        processable.append(r)
        segs = (r.raw_transcript or {}).get("segments", [])
        expected = {w["start"] for w in _chunk_transcript(segs)} if segs else set()
        if expected - saved[(r.video_id, lid)]:
            candidates.append((r, len(expected & saved[(r.video_id, lid)]), len(expected)))
    pending = [r for r in reviews if r.status == ReviewStatus.PENDING.value]

    print(f"matched reviews: {len(matched)}  processable (exactly one effective config): "
          f"{len(processable)}")
    print(f"/process-bulk candidates (processable, some window unchunked): {len(candidates)}")
    for r, got, exp in candidates:
        print(f"    {r.video_id:<12} {got}/{exp} windows  {_title(r, 50)}")
    print(f"human queue: {len(pending)} pending + {len(queue_matched)} matched-but-unprocessable "
          f"= {len(pending) + len(queue_matched)}")
    for r in queue_matched:
        ls = links_by_review.get(r.id, [])
        print(f"    {r.video_id:<12} status=matched links={[(l.match_source, 'config' if l.laptop_id else 'family') for l in ls]}  "
              f"{_title(r, 45)}")


def audit_code() -> None:
    _heading("6", "Every code reference to matched_laptop_id")
    hits: dict[str, list[int]] = defaultdict(list)
    for d in _SCAN_DIRS:
        for path in sorted((_REPO / d).rglob("*.py")):
            if path.resolve() == Path(__file__).resolve():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _COLUMN.search(line):
                    hits[path.relative_to(_REPO).as_posix()].append(i)
    total = sum(len(v) for v in hits.values())
    print(f"{total} lines in {len(hits)} files (comments included; migrations are history):")
    for f, lines in hits.items():
        print(f"    {f}: {', '.join(map(str, lines))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.parse_args()

    # get_engine(), not the `engine` proxy: Session(proxy) opens a NEW pooled
    # connection per statement (it files the connection under the real engine
    # and looks it up by the proxy), so SET TRANSACTION READ ONLY would guard a
    # different connection than the queries -- and the per-review match_laptop
    # calls below exhaust the pool.
    with Session(get_engine()) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        reviews = session.exec(select(RawYoutubeReview)).all()
        chunks = session.exec(select(LaptopReviewChunk)).all()
        links = session.exec(select(ReviewLaptopLink)).all()

        reviews_by_video = {r.video_id: r for r in reviews}
        links_by_review: dict = defaultdict(list)
        for l in links:
            links_by_review[l.raw_review_id].append(l)

        print(f"raw reviews: {len(reviews)} {dict(Counter(r.status for r in reviews))}")
        print(f"links: {len(links)} "
              f"{dict(Counter((l.match_source, 'config' if l.laptop_id else 'family') for l in links))}"
              f" on {len(links_by_review)} reviews")

        audit_duplicates(chunks, reviews_by_video)
        audit_partial(chunks, reviews_by_video)
        audit_overwrite(session, reviews, links_by_review)
        audit_orphan_chunks(chunks, reviews_by_video, links_by_review)
        audit_link_rules(reviews, chunks, links_by_review)
        session.rollback()
    audit_code()


if __name__ == "__main__":
    main()
