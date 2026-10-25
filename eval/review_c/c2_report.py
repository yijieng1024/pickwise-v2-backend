"""
Review C2: score (a) current claims, (b) the new prompt, (c)/(d) Laya against
the hand labels in eval/review_c/out/labels.json. Reads only the out/ files;
no database, no model.

`about` is scored at family level (F2.C1 counts as F2): the labels name no
configuration. Windows labelled UNSURE for sentiment are left out of the
sentiment scores and listed.

Usage:
    python eval/review_c/c2_report.py                  # prints, and writes out/c2_report.md
    python eval/review_c/c2_report.py --label tuning   # (b) from c2_new_prompt_tuning.json
    python eval/review_c/c2_report.py --holdout        # held-out 20: current claims and both
                                                       # pinned models, against labels2.json
"""
import argparse
import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
FIXED = ("OTHER", "SEVERAL", "NONE")
SENTS = ("strength", "weakness", "neutral")
_REASON = {"other_laptop": "OTHER", "several": "SEVERAL", "none": "NONE"}


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def fam(about):
    return about.split(".")[0] if about and about.startswith("F") else about


def about_and_place(kind, r):
    """(answer, attached_family or None, unattached_why)"""
    if kind == "current":
        if r["claim"] == "chunk":
            return fam(r["about"]), fam(r["about"]), None
        if r["claim"] == "unattached":
            return _REASON.get(r["reason"], r["reason"]), None, r["reason"]
        return "NO_CLAIM", None, "no live claim (window never processed)"
    if kind == "prompt":
        if "failed" in r:
            return "FAILED", None, "call failed"
        p = r["placement"]
        if "reason" in p:
            return fam(r["about"]), None, p["reason"]
        return fam(r["about"]), fam(r["about"]), None
    ans = r["about"]
    return ans, (ans if ans not in FIXED else None), (None if ans not in FIXED else ans)


def sentiment(kind, r):
    if kind == "current":
        return r.get("sentiment") or "no claim"
    if kind == "prompt":
        return r.get("sentiment", "FAILED")
    return r["sentiment"]


MAX_FAILED = 5  # --holdout: more provider failures than this and a model is not scored


def provider_failed(kind, r):
    """Still unavailable (429 / 5xx / timeout) after c2_collect's retries: no answer
    to judge. A wrong answer (InvalidAnswer) is not this; it counts as wrong."""
    return kind == "prompt" and r.get("failed", "").startswith("LLMUnavailable")


def pct(k, n):
    return f"{k}/{n} = {100 * k / n:.0f}%" if n else "n=0"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="", help="score c2_new_prompt_<label>.json as (b)")
    parser.add_argument("--holdout", action="store_true", help="the held-out sample and its runs")
    args = parser.parse_args()
    if args.holdout:
        suffix = "_holdout"
        sample, labels = load("sample_holdout.json"), load("labels2.json")
        # (key, sentiment name, which-laptop name, kind, results)
        runs = [("a", "(a) current claims", "(a) current claims", "current", load("c2_current_holdout_gemma.json"))]
        for key, model in (("gemma", "gemma-4-31b-it"), ("flash", "gemini-3.5-flash")):
            name = f"new prompt, {model} only"
            runs.append((key, name, name, "prompt", load(f"c2_new_prompt_holdout_{key}.json")))
    else:
        suffix = f"_{args.label}" if args.label else ""
        sample, labels = load("sample.json"), load("labels.json")
        runs = [("a", "(a) current claims", "(a) current claims", "current", load("c2_current.json")),
                ("b", "(b) new prompt", "(b) new prompt", "prompt", load(f"c2_new_prompt{suffix}.json")),
                ("laya", "(c) Laya", "(d) Laya which laptop", "laya", load("c2_laya.json"))]
    if labels["sample_id"] != sample["sample_id"]:
        raise SystemExit("STOP: the labels are for another sample.")
    L = labels["labels"]
    W = {w["id"]: w for w in sample["windows"]}
    lines = []
    p = lines.append
    # --holdout: a window a model could not answer is reported as failed and left
    # out of that model's scores; past MAX_FAILED the model is not scored at all.
    failed_of, not_scored = {}, []
    for run in list(runs):
        gone = [w for w in L if provider_failed(run[3], run[4][w])] if args.holdout else []
        failed_of[run[0]] = gone
        if len(gone) > MAX_FAILED:
            not_scored.append(run)
            runs.remove(run)
    scored = {run[0]: [w for w in L if w not in failed_of[run[0]]] for run in runs}
    if args.holdout:
        p("# Held-out sample: not used to write the rubric\n")
        for key, _, name, _, _ in runs + not_scored:
            p(f"- {name}: {len(failed_of[key])} of {len(L)} windows still failed after 3 retries"
              + (f" ({', '.join(f'`{w}`' for w in failed_of[key])}), not scored" if failed_of[key] else ""))
        for _, _, name, _, _ in not_scored:
            p(f"- **{name} is NOT SCORED**: more than {MAX_FAILED} windows failed.")
        p("")
    if args.label == "tuning":
        p("# TUNING RUN: these 40 windows were used to rewrite the rubric after seeing the "
          "model's answers, so this is not a fair test. The held-out sample is.\n")

    def title(w):
        return f"`{w}` ({W[w]['language']}) {W[w]['video_title'][:60]}"

    # ---------------- which laptop
    p("## Which laptop (family level)\n")
    n = len(L)
    base = sum(L[w]["about"] == "F1" for w in L)
    p(f"Baseline, always answer F1: {pct(base, n)}\n")
    p("| method | all | English | Chinese |" + (" F1 baseline on the same windows |" if args.holdout else "")
      + "\n|---|---|---|---|" + ("---|" if args.holdout else ""))
    for key, _, name, kind, r in runs:
        cells = []
        for lang in (None, "en", "zh"):
            ws = [w for w in scored[key] if lang is None or W[w]["language"] == lang]
            ok = sum(about_and_place(kind, r[w])[0] == L[w]["about"] for w in ws)
            cells.append(pct(ok, len(ws)))
        if args.holdout:
            cells.append(pct(sum(L[w]["about"] == "F1" for w in scored[key]), len(scored[key])))
        p(f"| {name} | " + " | ".join(cells) + " |")
    zh = sum(W[w]["language"] == "zh" for w in L)
    p(f"\nF1 baseline by language: English {pct(sum(L[w]['about'] == 'F1' for w in L if W[w]['language'] == 'en'), n - zh)}, "
      f"Chinese {pct(sum(L[w]['about'] == 'F1' for w in L if W[w]['language'] == 'zh'), zh)}\n")

    unatt_h = [w for w in L if L[w]["about"] in FIXED]
    p(f"### Wrong public claims: you said OTHER / SEVERAL / NONE, the method attached it to a linked laptop\n")
    p(f"Only **{len(unatt_h)} of {n} windows** are labelled OTHER / SEVERAL / NONE "
      f"({dict(Counter(L[w]['about'] for w in unatt_h))}). One window moves a rate by "
      f"{100 / max(1, len(unatt_h)):.0f} points; read these as counts, not rates.\n")
    for key, _, name, kind, r in runs:
        mine = [w for w in unatt_h if w in scored[key]]
        bad = [w for w in mine if about_and_place(kind, r[w])[1]]
        p(f"- **{name}: {len(bad)} of {len(mine)}**")
        for w in bad:
            p(f"    - {title(w)}: you {L[w]['about']}, method attached to {about_and_place(kind, r[w])[1]}")
    p("")
    p("### Wrong family: you named a linked family, the method attached it to a different one\n")
    for key, _, name, kind, r in runs:
        bad = [w for w in scored[key] if L[w]["about"] not in FIXED
               and about_and_place(kind, r[w])[1] not in (None, L[w]["about"])]
        p(f"- **{name}: {len(bad)}**")
        for w in bad:
            p(f"    - {title(w)}: you {L[w]['about']}, method {about_and_place(kind, r[w])[1]}")
    p("")
    att_h = [w for w in L if L[w]["about"] not in FIXED]
    p(f"### Lost content: you attached it to a laptop ({len(att_h)} windows), the method left it unattached\n")
    for key, _, name, kind, r in runs:
        mine = [w for w in att_h if w in scored[key]]
        lost = [w for w in mine if about_and_place(kind, r[w])[1] is None]
        p(f"- **{name}: {len(lost)} of {len(mine)}**")
        for w in lost:
            ans, _, why = about_and_place(kind, r[w])
            p(f"    - {title(w)}: you {L[w]['about']}, method {ans} ({why})")
    p("")

    # ---------------- sentiment
    p("## Sentiment\n")
    S = [w for w in L if L[w]["sentiment"] != "UNSURE"]
    unsure = [w for w in L if L[w]["sentiment"] == "UNSURE"]
    common, k = Counter(L[w]["sentiment"] for w in S).most_common(1)[0]
    p(f"{len(unsure)} windows labelled UNSURE are left out, so n = {len(S)}. "
      f"Baseline, always '{common}': {pct(k, len(S))}\n")
    p("| method | all | English | Chinese |"
      + (f" '{common}' baseline on the same windows |" if args.holdout else "")
      + "\n|---|---|---|---|" + ("---|" if args.holdout else ""))
    for key, name, _, kind, r in runs:
        mine = [w for w in S if w in scored[key]]
        cells = []
        for lang in (None, "en", "zh"):
            ws = [w for w in mine if lang is None or W[w]["language"] == lang]
            cells.append(pct(sum(sentiment(kind, r[w]) == L[w]["sentiment"] for w in ws), len(ws)))
        if args.holdout:
            cells.append(pct(sum(L[w]["sentiment"] == common for w in mine), len(mine)))
        p(f"| {name} | " + " | ".join(cells) + " |")
    zs = [w for w in S if W[w]["language"] == "zh"]
    p(f"\nMost-common baseline by language: English "
      f"{pct(sum(L[w]['sentiment'] == common for w in S if W[w]['language'] == 'en'), len(S) - len(zs))}, "
      f"Chinese {pct(sum(L[w]['sentiment'] == common for w in zs), len(zs))}\n")
    for key, name, _, kind, r in runs:
        mine = [w for w in S if w in scored[key]]
        cols = list(SENTS) + sorted({sentiment(kind, r[w]) for w in mine} - set(SENTS))
        p(f"**{name}**: rows are your label, columns the method's\n")
        p("| you \\ method | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols))
        for s in SENTS:
            row = Counter(sentiment(kind, r[w]) for w in mine if L[w]["sentiment"] == s)
            p(f"| {s} | " + " | ".join(str(row[c]) for c in cols) + " |")
        p("")
    if unsure:
        p("UNSURE windows, for reference: " + "; ".join(
            f"{title(w)} " + " ".join(f"{key}={sentiment(kind, r[w])}" for key, _, _, kind, r in runs)
            for w in unsure) + "\n")

    # ---------------- cost
    p("## Time per window\n")
    for key, name, _, kind, r in runs + not_scored:
        if kind == "laya":
            for dev in ("cuda", "cpu"):
                for lang in ("en", "zh"):
                    ms = sorted(x["ms"][dev] for w, x in r.items()
                                if dev in x.get("ms", {}) and W[w]["language"] == lang)
                    if ms:
                        p(f"- Laya {dev} {lang} ({r[next(iter(r))]['models'][lang]}): "
                          f"median {ms[len(ms) // 2]:.0f} ms, max {ms[-1]:.0f} ms, n={len(ms)}")
        elif kind == "prompt":
            secs = sorted(x["seconds"] for x in r.values())
            p(f"- {name} (Gemini API, not this PC): median {secs[len(secs) // 2]:.1f} s per call, "
              "plus the 4 s pacing delay")
            if any("retries" in x for x in r.values()):
                tally = Counter(x.get("retries", 0) for x in r.values())
                p(f"- {name} retries (429/500/503/504) per window: "
                  + ", ".join(f"{k} retries: {v}" for k, v in sorted(tally.items())))
            failed = [w for w, x in r.items() if "failed" in x]
            if failed:
                p(f"- {name} failures: " + "; ".join(f"`{w}` {r[w]['failed']}" for w in failed))

    report = "\n".join(lines)
    (OUT / f"c2_report{suffix}.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
