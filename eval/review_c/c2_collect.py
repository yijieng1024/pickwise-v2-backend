"""
Review C2: collect (a) the current live claim and (b) the new prompt's answer
for every window of the labelling sample. Read-only against DATABASE_URL: the
session is a READ ONLY transaction and is closed before the first model call;
the chain is built with feature=None, so no usage-log row is written.

Stops if a window's candidate list no longer matches the one on the labelling
page (links changed since the sample was built), because the labels' ids
would then mean different laptops.

A model call that fails with 429 / 500 / 503 / 504 is retried up to 3 times (30 s,
60 s, 120 s backoff); each result records `retries`. Safe only because this
script writes nothing to the database.

Writes eval/review_c/out/c2_current.json and c2_new_prompt[_<label>].json, the
latter after every window: re-running resumes, retrying only failed windows.

Usage:
    python eval/review_c/c2_collect.py [--label tuning] [--sample sample.json]
    python eval/review_c/c2_collect.py --sample sample_holdout.json --label holdout_flash \
        --model google_genai:gemini-3.5-flash     # that one model, no fallback
"""
import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.common import llm_config  # noqa: E402
from app.common.llm import LLMUnavailable, build_llm  # noqa: E402
from app.database import get_engine  # noqa: E402
from app.reviews import processor  # noqa: E402
from app.scripts.reprocess_reviews import _processable  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
_RETRYABLE = re.compile(r"\b(429|500|503|504)\b")
_BACKOFF_S = (30, 60, 120)


def analyse_with_retry(chain, cands, title, window, wid, sleep=time.sleep) -> dict:
    """One window's result, with `retries`. Only the provider's 429 / 500 / 503 / 504
    is retried, which is safe because this script writes nothing to the
    database; a bad answer (InvalidAnswer) is never retried."""
    retries = 0
    while True:
        try:
            analysis, about, placement = processor.analyse_window(chain, cands, title, window)
            return {"about": about, "placement": {k: str(v) if v else v for k, v in placement.items()},
                    **analysis.model_dump(exclude={"about"}), "retries": retries}
        except Exception as e:
            if isinstance(e, LLMUnavailable) and _RETRYABLE.search(str(e)) and retries < len(_BACKOFF_S):
                print(f"    {wid}: {processor._first_line(e)[:80]}; retry in {_BACKOFF_S[retries]}s")
                sleep(_BACKOFF_S[retries])
                retries += 1
                continue
            return {"failed": f"{type(e).__name__}: {processor._first_line(e)}", "retries": retries}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="", help="suffix for the new-prompt output file")
    parser.add_argument("--sample", default="sample.json", help="sample file in out/")
    parser.add_argument("--model", default=None, help="pin one 'provider:model', no fallback "
                        "(default: the configured review_processor chain)")
    args = parser.parse_args()
    suffix = f"_{args.label}" if args.label else ""
    sample = json.loads((OUT / args.sample).read_text(encoding="utf-8"))
    wanted = {w["id"]: w for w in sample["windows"]}
    vids = sorted({w["video_id"] for w in wanted.values()})

    work, current = [], {}
    with Session(get_engine()) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        for _, vid, title, segments, links in _processable(session, vids):
            cands = processor.build_candidates(session, links)
            by_ref = {ref: cid for cid, ref in cands.ids.items()}
            for w in processor.windows_with_context(segments):
                wid = f"{vid}@{w['start']}"
                if wid not in wanted:
                    continue
                if cands.text != wanted[wid]["candidates_text"]:
                    raise SystemExit(f"STOP: candidate list of {wid} changed since the sample was built.")
                work.append((wid, title, cands, w))
                chunk = session.execute(text(
                    "SELECT family_id, laptop_id, scope, sentiment_tag FROM laptop_review_chunks "
                    "WHERE video_id = :v AND timestamp_start_seconds = :s"), {"v": vid, "s": w["start"]}
                ).first()
                unattached = session.execute(text(
                    "SELECT reason, sentiment_tag FROM review_unattached_claims "
                    "WHERE video_id = :v AND timestamp_start_seconds = :s"), {"v": vid, "s": w["start"]}
                ).first()
                if chunk:
                    about = by_ref.get((chunk.family_id, chunk.laptop_id)) or by_ref.get((chunk.family_id, None))
                    current[wid] = {"claim": "chunk", "about": about or "UNLISTED_FAMILY",
                                    "scope": chunk.scope, "sentiment": chunk.sentiment_tag}
                elif unattached:
                    current[wid] = {"claim": "unattached", "reason": unattached.reason,
                                    "sentiment": unattached.sentiment_tag}
                else:
                    current[wid] = {"claim": None}
    missing = set(wanted) - {wid for wid, *_ in work}
    if missing:
        raise SystemExit(f"STOP: sample windows no longer processable: {sorted(missing)}")
    (OUT / f"c2_current{suffix}.json").write_text(json.dumps(current, indent=1), encoding="utf-8")
    print(f"current claims: {len(current)} windows written")

    if args.model:
        llm = build_llm(llm_config.REVIEW_PROCESSOR, temperature=0, models=(args.model, []))
        chain = ChatPromptTemplate.from_messages(
            [("system", processor._SYSTEM_PROMPT), ("human", processor._HUMAN_PROMPT)]
        ) | llm.with_structured_output(processor._WindowAnalysis)
    else:
        chain = processor.build_chain(feature=None)
    # Written after every window, so a stopped run keeps what it did; a re-run
    # skips windows already answered and retries the ones that failed.
    out_file = OUT / f"c2_new_prompt{suffix}.json"
    results = json.loads(out_file.read_text(encoding="utf-8")) if out_file.exists() else {}
    for i, (wid, title, cands, w) in enumerate(work, start=1):
        if wid in results and "failed" not in results[wid]:
            continue
        started = time.monotonic()
        results[wid] = analyse_with_retry(chain, cands, title, w, wid)
        results[wid]["seconds"] = round(time.monotonic() - started, 2)  # includes backoff
        results[wid]["model"] = args.model or "configured chain"
        print(f"[{i}/{len(work)}] {wid}: {results[wid].get('about', results[wid].get('failed'))}")
        tmp = out_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out_file)
        time.sleep(processor._INTER_REQUEST_DELAY)
    print(f"new prompt: {sum('failed' not in r for r in results.values())}/{len(results)} answered, "
          f"retries per window: {dict(sorted(Counter(r['retries'] for r in results.values()).items()))}")


if __name__ == "__main__":
    main()
