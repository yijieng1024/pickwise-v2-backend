"""
Review C2: draw the stratified 40-window accuracy sample and build the blind
labelling page. Read-only against DATABASE_URL; calls no model.

    multi_family   13 windows, at most 2 per video (reviews linked to 2+ families)
    known           6 windows, 2 each from TEJdZf31x6s, IOZrScopvLE, lotIrC6zzmU
    single_family  21 windows, at most 2 per video
    Chinese        at least 8 of the 40, at most 3 per Chinese video

Two fields are labelled: which laptop (`about`) and sentiment. Topic and scope
are spot-checked separately.

Fixed seed, so the same links give the same sample. The candidate list on each
window is built by the processor's own build_candidates, and the page shows the
processor's own rubric strings, so the human labels and the model are judged
against one set of ids and definitions. No model output appears on the page.

Writes eval/review_c/out/sample.json and eval/review_c/out/labeling.html
(gitignored: transcript text, and later your labels). Open the HTML from disk;
"Export labels" saves labels.json for the C2 comparison.

--holdout draws a fresh 20 (multi_family 7, known 3, single_family 10, at
least 5 Chinese) from windows not in sample.json, with its own seed, and
writes sample_holdout.json / labeling_holdout.html.

Usage:
    python eval/review_c/build_sample.py [--holdout]
"""
import argparse
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.database import get_engine  # noqa: E402
from app.reviews import processor  # noqa: E402
from app.reviews.link_service import effective_links  # noqa: E402
from app.scripts.reprocess_reviews import _processable  # noqa: E402

SEED = 20261023
KNOWN = ("TEJdZf31x6s", "IOZrScopvLE", "lotIrC6zzmU")
QUOTA = {"multi_family": 13, "known": 6, "single_family": 21}
CAP = {"multi_family": 2, "known": 2, "single_family": 2}
ZH_CAP, ZH_MIN = 3, 8
HOLDOUT_SEED = 20261024
HOLDOUT_QUOTA = {"multi_family": 7, "known": 3, "single_family": 10}
HOLDOUT_ZH_MIN = 5
HERE = Path(__file__).resolve().parent
_CJK = re.compile(r"[㐀-鿿]")


def _is_zh(window_text: str) -> bool:
    return len(_CJK.findall(window_text)) > 0.2 * max(1, len(window_text.replace(" ", "")))


def load_pool() -> dict:
    """stratum -> video_id -> [window dicts]"""
    pool: dict = defaultdict(lambda: defaultdict(list))
    with Session(get_engine()) as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        for _, vid, title, segments, links in _processable(session):
            cands = processor.build_candidates(session, links)
            families = {l.family_id for l in effective_links(links)}
            stratum = ("known" if vid in KNOWN
                       else "multi_family" if len(families) > 1 else "single_family")
            for w in processor.windows_with_context(segments):
                pool[stratum][vid].append({
                    "id": f"{vid}@{w['start']}",
                    "video_id": vid,
                    "video_title": title,
                    "start": w["start"],
                    "end": w["end"],
                    "url": f"https://youtube.com/watch?v={vid}&t={w['start']}",
                    "language": "zh" if _is_zh(w["text"]) else "en",
                    "stratum": stratum,
                    "candidates_text": cands.text,
                    "candidate_ids": [[k, cands.names[k]] for k in cands.ids],
                    "previous": w["previous"],
                    "text": w["text"],
                })
    return pool


def draw(pool: dict, seed=SEED, quota=QUOTA, zh_min=ZH_MIN, exclude=frozenset()) -> list[dict]:
    rng = random.Random(seed)
    remaining = {}
    for s in sorted(pool):
        for v, ws in sorted(pool[s].items()):
            ws = [w for w in ws if w["id"] not in exclude]
            remaining[(s, v)] = rng.sample(ws, len(ws))
    taken: dict = defaultdict(int)
    count: dict = defaultdict(int)
    picked: list[dict] = []

    def is_zh_video(key):
        ws = pool[key[0]][key[1]]
        return sum(w["language"] == "zh" for w in ws) > len(ws) / 2

    def cap(key):
        return ZH_CAP if is_zh_video(key) and key[0] != "known" else CAP[key[0]]

    def fill(keys, stop):
        progress = True
        while progress and not stop():
            progress = False
            for key in keys:
                if stop():
                    break
                if count[key[0]] < quota[key[0]] and taken[key] < cap(key) and remaining[key]:
                    picked.append(remaining[key].pop())
                    taken[key] += 1
                    count[key[0]] += 1
                    progress = True

    zh_keys = [k for k in remaining if is_zh_video(k)]
    rng.shuffle(zh_keys)
    fill(zh_keys, lambda: sum(w["language"] == "zh" for w in picked) >= zh_min)
    for stratum in quota:
        keys = [k for k in remaining if k[0] == stratum]
        rng.shuffle(keys)
        fill(keys, lambda: count[stratum] >= quota[stratum])
    rng.shuffle(picked)  # blind to stratum order
    return picked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--holdout", action="store_true", help="fresh 20, none from sample.json")
    args = parser.parse_args()
    out = HERE / "out"
    sample_file, page_file = (
        ("sample_holdout.json", "labeling_holdout.html") if args.holdout else ("sample.json", "labeling.html")
    )
    if args.holdout:
        used = {w["id"] for w in json.loads((out / "sample.json").read_text(encoding="utf-8"))["windows"]}
        sample = draw(load_pool(), HOLDOUT_SEED, HOLDOUT_QUOTA, HOLDOUT_ZH_MIN, frozenset(used))
        assert not used & {w["id"] for w in sample}
    else:
        sample = draw(load_pool())
    sample_id = hashlib.sha256(json.dumps([w["id"] for w in sample]).encode()).hexdigest()[:12]
    rubric = {
        "about": processor.ABOUT_RULE,
        "fixed_answers": processor.FIXED_ANSWERS,
        "sentiment": processor.SENTIMENT_RULE,
    }
    out.mkdir(exist_ok=True)
    payload = {"sample_id": sample_id, "seed": HOLDOUT_SEED if args.holdout else SEED,
               "rubric": rubric, "windows": sample}
    (out / sample_file).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    page = (HERE / "labeling_template.html").read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    (out / page_file).write_text(page.replace("__SAMPLE_JSON__", data), encoding="utf-8")

    by = defaultdict(int)
    for w in sample:
        by[w["stratum"]] += 1
    zh = sum(w["language"] == "zh" for w in sample)
    print(f"sample {sample_id}: {len(sample)} windows {dict(by)}, Chinese {zh}, "
          f"videos {len({w['video_id'] for w in sample})}")
    print(f"wrote {out / sample_file}\nwrote {out / page_file}")


if __name__ == "__main__":
    main()
