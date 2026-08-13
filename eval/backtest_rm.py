"""Replay the RM-grounding check over past run files without spending quota.

Run this before trusting rm_figures_grounded: a check that fires on honest
answers gets ignored, and an ignored check is worse than no check.

Usage:
  python eval/backtest_rm.py eval/runs/2026-08-13_v2_checks.jsonl
  python eval/backtest_rm.py "eval/runs/*.jsonl"     # quote it — see below
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_eval import _ungrounded_rm  # noqa: E402

# PowerShell does not expand wildcards for arguments the way a POSIX shell
# does — the pattern arrives verbatim and open() fails with EINVAL. Expand
# here so a quoted glob works on either shell.
paths = [p for arg in sys.argv[1:] for p in (sorted(glob.glob(arg)) or [arg])]
if not paths:
    sys.exit(__doc__)

for path in paths:
    print(f"===== {path}")
    hits = turns = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        # Mirror run_one: ground truth is cumulative across the conversation,
        # so a turn answered from the shortlist pool (no tool_outputs of its
        # own) is still checked against the earlier turn's results, and the
        # user's own words count as grounding for a budget echoed back.
        tool_texts: list[str] = []
        user_texts: list[str] = []
        for i, t in enumerate(d.get("turns", []), 1):
            tool_texts.extend(t.get("tool_outputs") or [])
            user_texts.append(t.get("query") or "")
            if not tool_texts:  # nothing retrieved yet — no ground truth
                continue
            turns += 1
            bad = _ungrounded_rm(t["response"], tool_texts, user_texts)
            if bad:
                hits += 1
                print(f"  {d['id']} turn {i} → {bad}")
    print(f"  {hits} flagged / {turns} checked turns")
