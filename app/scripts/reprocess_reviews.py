"""
Review C: re-process every processable review into review_claims_staging, then
swap all videos into the live tables in one transaction.

    run              DRY RUN: analyse a few windows with the real prompt and
                     print where each claim would go. Calls Gemini, writes
                     nothing (no claims, no embedding, no usage-log row).
    run --apply      process every window of every processable review into
                     staging. Resumable: re-running continues where it
                     stopped. The live chunks are untouched. Stops before
                     a review that would take the run past --max-windows
                     (default 400): every window costs one embedding call,
                     and gemini-embedding-2's free tier is 1,000 per day,
                     shared with the agent's search. Re-run the same
                     command the next day to continue.
    swap             DRY RUN: per video, staged vs expected windows, and what
                     the swap would move.
    swap --apply     exchange live and staged claims for every staged video in
                     one transaction. The live rows move into staging, so
                     running `swap --apply` again rolls back.

A review is processable when it is matched, has at least one effective link
and a transcript. Do not press /reviews/process-bulk while `run --apply` is
going; it would process the same windows a second time. Targets DATABASE_URL.

Usage:
    python -m app.scripts.reprocess_reviews run [--video ID ...] [--windows 8]
    python -m app.scripts.reprocess_reviews run --apply [--max-reviews N] [--max-windows 400]
    python -m app.scripts.reprocess_reviews swap [--apply]
"""
import argparse
import time

from sqlalchemy import text
from sqlmodel import Session, select

from app.database import get_engine
from app.reviews import processor
from app.reviews.link_service import effective_links, links_by_review
from app.reviews.models import (
    LaptopReviewChunk,
    RawYoutubeReview,
    ReviewClaimStaging,
    ReviewStatus,
    ReviewUnattachedClaim,
)


def _processable(session: Session, video_ids=None) -> list[tuple]:
    """(id, video_id, title, segments, links) of every processable review.
    Column selects only, so the dry run also works before the Review C
    migration is applied."""
    statement = (
        select(RawYoutubeReview.id, RawYoutubeReview.video_id, RawYoutubeReview.video_title,
               RawYoutubeReview.raw_transcript)
        .where(RawYoutubeReview.status == ReviewStatus.MATCHED.value)
        .order_by(RawYoutubeReview.created_at, RawYoutubeReview.id)  # type: ignore[arg-type]
    )
    if video_ids:
        statement = statement.where(RawYoutubeReview.video_id.in_(video_ids))  # type: ignore[attr-defined]
    rows = session.exec(statement).all()
    links = links_by_review(session, [r[0] for r in rows])
    return [
        (rid, vid, title, (tr or {}).get("segments", []), links[rid])
        for rid, vid, title, tr in rows
        if effective_links(links[rid]) and (tr or {}).get("segments")
    ]


def _where(placement: dict, cands: processor.Candidates) -> str:
    if "reason" in placement:
        return f"UNATTACHED {placement['reason']}"
    names = {v: k for k, v in cands.ids.items()}
    fam = names.get((placement["family_id"], None), "?")
    cfg = names.get((placement["family_id"], placement["laptop_id"])) if placement["laptop_id"] else None
    target = cands.names.get(cfg or fam, "?")
    if placement["laptop_id"] and not cfg:
        target += " (the link's / only configuration)"
    return f"{placement['scope']:<6} -> {target}"


def run_dry(video_ids, max_windows: int) -> None:
    with Session(get_engine()) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        work = []
        for rid, vid, title, segments, links in _processable(session, video_ids):
            cands = processor.build_candidates(session, links)
            for w in processor.windows_with_context(segments):
                work.append((vid, title, cands, w))
    # The session is closed before the first Gemini call.
    work = work[::max(1, len(work) // max_windows)][:max_windows]  # spread across videos
    # Records usage: the dry run writes no claims but spends real quota.
    chain = processor.build_chain()
    shown_cands = set()
    for vid, title, cands, w in work:
        if vid not in shown_cands:
            print(f"\n=== {vid}  {title[:90]}\n{cands.text}")
            shown_cands.add(vid)
        try:
            analysis, about, placement = processor.analyse_window(chain, cands, title, w)
            print(f"\n@{w['start']}s  about={about} scope={analysis.scope} topic={analysis.topic} "
                  f"sentiment={analysis.sentiment}"
                  + (f" other_laptop={analysis.other_laptop!r}" if analysis.other_laptop else ""))
            print(f"  -> {_where(placement, cands)}")
            print(f"  summary: {analysis.summary}")
        except Exception as e:  # reported, never retried here
            print(f"\n@{w['start']}s  FAILED {type(e).__name__}: {processor._first_line(e)}")
        time.sleep(processor._INTER_REQUEST_DELAY)
    print(f"\nDRY RUN: {len(work)} windows analysed, nothing written.")


def run_apply(video_ids, max_reviews, max_windows) -> None:
    with Session(get_engine()) as session:
        rows = _processable(session, video_ids)[:max_reviews]
        staged = processor._saved_windows(session, [r[1] for r in rows], staging=True)
        todo = [(rid, vid, sum(w["start"] not in staged.get(vid, set())
                               for w in processor.windows_with_context(seg)))
                for rid, vid, _, seg, _ in rows]
    totals = {"saved": 0, "failed": 0, "complete": 0}
    spent = 0  # windows sent to the model this run, i.e. embedding calls at most
    started = time.monotonic()
    for i, (rid, vid, remaining) in enumerate(todo, start=1):
        if spent + remaining > max_windows:
            left = sum(r for *_, r in todo[i - 1:])
            print(f"\nDAILY CAP: {vid} needs {remaining} windows, {spent}/{max_windows} used. "
                  f"Stopped; {left} windows left. Re-run the same command tomorrow.")
            break
        spent += remaining
        with Session(get_engine()) as session:
            try:
                r = processor.process_raw_review(rid, session, stage_only=True)
            except processor.AlreadyProcessed:
                totals["complete"] += 1
                print(f"[{i}/{len(todo)}] {vid}: already fully staged")
                continue
            except ValueError as e:
                print(f"[{i}/{len(todo)}] {vid}: skipped: {e}")
                continue
        totals["saved"] += r["chunks_saved"]
        totals["failed"] += r["chunks_failed"]
        totals["complete"] += r["complete"]
        mins = (time.monotonic() - started) / 60
        print(f"[{i}/{len(todo)}] {vid}: {r['chunks_saved']} staged, {r['chunks_failed']} failed, "
              f"{r['windows_already_saved']} already staged, complete={r['complete']}  ({mins:.0f} min)")
        for f in r["failures"]:
            print(f"    @{f['start_seconds']}s {f['error_type']}: {f['error']}")
    print(f"\nAPPLIED to staging: {totals['saved']} windows staged, {totals['failed']} failed, "
          f"{totals['complete']}/{len(todo)} reviews complete. Re-run to retry failures; "
          "then `swap` (dry run) and `swap --apply`.")


def swap(apply: bool) -> None:
    with Session(get_engine()) as session:
        if not apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        expected = {
            vid: {w["start"] for w in processor._chunk_transcript(segments)}
            for _, vid, _, segments, _ in _processable(session)
        }
        staged = processor._saved_windows(session, staging=True)
        live = processor._saved_windows(session)
        incomplete, orphaned = [], []
        print(f"{'video':<12} {'staged':>6} {'expected':>8} {'live':>5}")
        for vid in sorted(staged):
            exp = expected.get(vid)
            n_live = len(live.get(vid, set()))
            if exp is None:
                orphaned.append(vid)  # no longer processable
            elif not exp <= staged[vid]:
                incomplete.append(vid)
            print(f"{vid:<12} {len(staged[vid]):>6} {len(exp) if exp else '-':>8} {n_live:>5}")
        unstaged = sorted(set(expected) - set(staged))

        def count(model, vids, extra=None):
            q = select(model.id).where(model.video_id.in_(vids))
            return len(session.exec(q.where(extra) if extra is not None else q).all())

        vids = sorted(staged)
        print(f"\n{len(vids)} staged videos. The swap would move into the live tables "
              f"{count(ReviewClaimStaging, vids, ReviewClaimStaging.reason.is_(None))} chunks and "
              f"{count(ReviewClaimStaging, vids, ReviewClaimStaging.reason.is_not(None))} unattached "
              f"claims, and move {count(LaptopReviewChunk, vids)} live chunks and "
              f"{count(ReviewUnattachedClaim, vids)} unattached claims into staging.")
        if unstaged:
            print(f"{len(unstaged)} processable videos have no staged claims and keep their live "
                  f"rows: {', '.join(unstaged)}")
        if incomplete or orphaned:
            print(f"REFUSED: incomplete staged sets {incomplete}; staged but no longer processable "
                  f"{orphaned}. Finish `run --apply` first.")
            return
        if not apply:
            print("DRY RUN: nothing written. Re-run with --apply.")
            return
        processor.swap_claims(session, vids, keep_previous=True)
        session.commit()
        print(f"APPLIED: swapped {len(vids)} videos. Staging now holds the previous claims; "
              "`swap --apply` again rolls back.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("--apply", action="store_true", help="write to staging (default: dry run)")
    r.add_argument("--video", nargs="*", help="only these video ids")
    r.add_argument("--windows", type=int, default=8, help="dry run: windows to analyse")
    r.add_argument("--max-reviews", type=int, default=None, help="--apply: stop after N reviews")
    r.add_argument("--max-windows", type=int, default=400,
                   help="--apply: daily cap on windows (= embedding calls), stops before a review that would exceed it")
    s = sub.add_parser("swap")
    s.add_argument("--apply", action="store_true", help="swap (default: dry run)")
    args = parser.parse_args()

    if args.command == "swap":
        swap(args.apply)
    elif args.apply:
        run_apply(args.video, args.max_reviews, args.max_windows)
    else:
        run_dry(args.video, args.windows)


if __name__ == "__main__":
    main()
