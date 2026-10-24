"""
Review C2: (c) Laya sentiment and (d) Laya "which laptop" on the labelling
sample. Reads only eval/review_c/out/sample.json; no database, no app imports,
so it runs in a separate venv with `laya` installed:

    <laya-venv>/Scripts/python eval/review_c/c2_laya.py

English windows go to the typed-decisions checkpoint, Chinese windows to
multilingual. The choices offered are the review's linked families plus
OTHER / SEVERAL / NONE: the labels name no configuration, and configuration
lines would overrun Laya's option budget. The definitions are the processor's
rubric strings, copied from the sample payload, so all methods share them.

Each window is timed on CUDA (if present) and on CPU, after one warm-up call
per model; model load time is reported separately. Writes out/c2_laya.json.
"""
import json
import time
from pathlib import Path

import laya
import torch

OUT = Path(__file__).resolve().parent / "out"
CHECKPOINT = {"en": "typed-decisions", "zh": "multilingual"}


def questions(window: dict, rubric: dict) -> tuple[dict, dict]:
    labels = {}  # model-facing label -> answer id
    criteria = {}
    for cid, name in window["candidate_ids"]:
        if "." in cid:
            continue
        labels[name] = cid
        criteria[name] = f"the excerpt's main point is about the {name}"
    for fixed, meaning in rubric["fixed_answers"].items():
        label = {"OTHER": "another laptop", "SEVERAL": "several laptops", "NONE": "no laptop"}[fixed]
        labels[label] = fixed
        criteria[label] = meaning
    about_rule = rubric["about"].split(" Use a configuration id")[0] + " " + \
        rubric["about"].split("otherwise use the family id (like F2). ")[-1]
    return {
        "about": {"type": "choice", "instructions": about_rule, "criteria": criteria},
        "sentiment": {"type": "choice", "instructions": rubric["sentiment"], "criteria": {
            "strength": "the reviewer is positive about the main point",
            "weakness": "the reviewer is critical of it or warns about it",
            "neutral": "facts, specs, setup, prices without judgement, or sponsor talk",
        }},
    }, labels


def state(window: dict) -> dict:
    return {"video_title": window["video_title"],
            "previous_excerpt_context_only": window["previous"],
            "excerpt": window["text"]}


def main() -> None:
    sample = json.loads((OUT / "sample.json").read_text(encoding="utf-8"))
    windows, rubric = sample["windows"], sample["rubric"]
    devices = (["cuda"] if torch.cuda.is_available() else []) + ["cpu"]
    results = {w["id"]: {"ms": {}, "models": CHECKPOINT} for w in windows}
    load_s = {}
    for dev in devices:
        for lang, ckpt in CHECKPOINT.items():
            started = time.monotonic()
            agent = laya.load("convaiinnovations/laya", subfolder=ckpt, device=dev)
            load_s[f"{dev}/{ckpt}"] = round(time.monotonic() - started, 1)
            mine = [w for w in windows if w["language"] == lang]
            if not mine:
                continue
            q, _ = questions(mine[0], rubric)
            agent.predict(state(mine[0]), q)  # warm-up
            for w in mine:
                q, labels = questions(w, rubric)
                started = time.perf_counter()
                out = agent.predict(state(w), q)
                if dev == "cuda":
                    torch.cuda.synchronize()
                results[w["id"]]["ms"][dev] = (time.perf_counter() - started) * 1000
                a = out["answers"]
                answer = {"about": labels[a["about"]["choice"]], "sentiment": a["sentiment"]["choice"],
                          "about_confidence": a["about"].get("confidence"),
                          "sentiment_confidence": a["sentiment"].get("confidence")}
                prev = results[w["id"]].get("answer")
                if prev and (prev["about"], prev["sentiment"]) != (answer["about"], answer["sentiment"]):
                    results[w["id"]]["differs_on_" + dev] = answer
                results[w["id"]].setdefault("answer", answer)
            del agent
            if dev == "cuda":
                torch.cuda.empty_cache()
    flat = {wid: {**r.pop("answer"), **r} for wid, r in results.items()}
    (OUT / "c2_laya.json").write_text(json.dumps({**flat}, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "c2_laya_load.json").write_text(json.dumps(load_s, indent=1), encoding="utf-8")
    print("load seconds:", load_s)
    print("answered:", len(flat))


if __name__ == "__main__":
    main()
