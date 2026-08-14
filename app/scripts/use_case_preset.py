"""
Locate whatever supplies the five use-case PickScores shown on the product page
(General Use / Office & Study / Programming / Gaming / Creative Work).

`calculate_pick_score(product, user_pref=None, ...)` returns ONE general-mode
score, so the five presets come from somewhere else. This does not guess a
name: it searches by content, in three passes.

  Pass 1  Text search across app/ for the five UI labels and for
          use-case-ish identifiers. Whatever renders those labels has to
          mention them, unless they live in the frontend.
  Pass 2  Import every module under the pickscore/recommendation packages and
          dump any module-level dict that looks like a weight table -- keyed by
          use case, or keyed by the scoring factors.
  Pass 3  List route handlers whose path or name mentions use case / pickscore,
          so the serving endpoint is visible even if the weights are built
          inline rather than stored in a constant.

Usage (Windows PowerShell, from the repo root):
    python scripts\\find_use_case_presets.py
    python scripts\\find_use_case_presets.py --root app --also frontend/src
"""
import argparse
import importlib
import pkgutil
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

RULE = "=" * 68

# The exact strings on the product page. Whatever builds those tabs must
# contain them somewhere -- backend, or frontend if the maths is client-side.
UI_LABELS = [
    "General Use",
    "Office & Study",
    "Programming",
    "Gaming",
    "Creative Work",
    "Best fit",
]

# Identifier shapes worth flagging even when the labels live elsewhere.
IDENT_PATTERNS = [
    r"USE_CASE\w*",
    r"use_case\w*",
    r"useCase\w*",
    r"\w*PRESET\w*",
    r"\w*PRIORIT\w*",
    r"\w*_WEIGHTS?\b",
]

# Factor names seen in the breakdown output. A dict keyed by several of these
# is a weight table whatever it happens to be called.
FACTOR_NAMES = {
    "price", "cpu", "gpu", "ram_storage", "portability",
    "battery", "screen_size", "brand",
}

# Packages worth importing for pass 2. Missing ones are skipped silently.
IMPORT_ROOTS = [
    "app.pickscore",
    "app.recommendation",
    "app.laptops",
]


def pass1_text_search(roots: list[Path]) -> None:
    print(RULE)
    print("PASS 1 - TEXT SEARCH")
    print(RULE)

    label_re = re.compile("|".join(re.escape(s) for s in UI_LABELS))
    ident_re = re.compile("|".join(IDENT_PATTERNS))

    hits = 0
    for root in roots:
        if not root.exists():
            print(f"  (skipped, not found: {root})")
            continue
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".ts", ".tsx", ".js", ".jsx", ".json"):
                continue
            if any(part in {"node_modules", "__pycache__", ".next", "venv"}
                   for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if label_re.search(line) or ident_re.search(line):
                    hits += 1
                    print(f"  {path}:{i}")
                    print(f"      {line.strip()[:110]}")
    if not hits:
        print("  No matches. The five labels may be hardcoded in the frontend")
        print("  only -- rerun with --also pointing at the Next.js source.")
    print()


def _looks_like_weight_table(value) -> bool:
    """A dict is a weight table if its keys are use cases whose values are
    numeric maps, or if its keys are the scoring factors themselves."""
    if not isinstance(value, dict) or not value:
        return False
    keys = {str(k).lower().replace(" ", "_").replace("&", "") for k in value}
    if len(keys & {n.lower() for n in FACTOR_NAMES}) >= 3:
        return True
    for v in value.values():
        if isinstance(v, dict):
            sub = {str(k).lower() for k in v}
            if len(sub & FACTOR_NAMES) >= 3:
                return True
        if isinstance(v, (list, tuple)) and len(v) >= 4:
            return True
    return False


def pass2_import_scan() -> None:
    print(RULE)
    print("PASS 2 - MODULE CONSTANT SCAN")
    print(RULE)

    found = False
    for root_name in IMPORT_ROOTS:
        try:
            root_mod = importlib.import_module(root_name)
        except Exception as e:
            print(f"  (skipped {root_name}: {type(e).__name__}: {e})")
            continue

        module_names = [root_name]
        if hasattr(root_mod, "__path__"):
            module_names += [
                f"{root_name}.{m.name}"
                for m in pkgutil.iter_modules(root_mod.__path__)
            ]

        for mod_name in module_names:
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            for attr in dir(mod):
                if attr.startswith("_"):
                    continue
                try:
                    value = getattr(mod, attr)
                except Exception:
                    continue
                if _looks_like_weight_table(value):
                    found = True
                    print(f"\n  {mod_name}.{attr}")
                    for k, v in list(value.items())[:12]:
                        print(f"      {k}: {v}")
    if not found:
        print("  No module-level weight table found. The presets are probably")
        print("  built inline -- see pass 3 for the serving endpoint.")
    print()


def pass3_route_scan(root: Path) -> None:
    print(RULE)
    print("PASS 3 - ROUTE HANDLERS MENTIONING USE CASE / PICKSCORE")
    print(RULE)

    route_re = re.compile(r'@router\.(get|post|patch|put)\(\s*["\']([^"\']+)')
    interest = re.compile(r"use.?case|pick.?score|breakdown|score", re.I)

    if not root.exists():
        print(f"  (skipped, not found: {root})")
        print()
        return

    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            m = route_re.search(line)
            if not m:
                continue
            window = " ".join(lines[i:i + 12])
            if interest.search(m.group(2)) or interest.search(window):
                print(f"  {path}:{i + 1}   {m.group(1).upper()} {m.group(2)}")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--root", default="app", help="Backend package root.")
    p.add_argument("--also", default=None,
                   help="Extra directory to text-search, e.g. the Next.js src.")
    args = p.parse_args()

    backend = Path(args.root)
    roots = [backend]
    if args.also:
        roots.append(Path(args.also))

    pass1_text_search(roots)
    pass2_import_scan()
    pass3_route_scan(backend)

    print(RULE)
    print("If all three passes come up empty, the five scores are computed in")
    print("the frontend from a single payload. Check what the product page")
    print("fetches, then search the Next.js source for the label strings.")
    print(RULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())