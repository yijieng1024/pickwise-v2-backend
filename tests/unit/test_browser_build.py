"""playwright and patchright must want the same Chromium build.

The image installs Chromium once, with `playwright install`; patchright
(pulled in by scrapling[fetchers]) finds and reuses that build only when its
pinned version asks for the same revision. Bump one without the other and it
looks for a build that isn't there (or a second ~700 MB one gets installed).
"""
import importlib.util
import json
from pathlib import Path


def _browsers_json(package: str) -> Path:
    root = importlib.util.find_spec(package).submodule_search_locations[0]
    return Path(root) / "driver" / "package" / "browsers.json"


def _revisions(browsers_json: Path) -> dict[str, str]:
    browsers = json.loads(browsers_json.read_text(encoding="utf-8"))["browsers"]
    return {b["name"]: b["revision"] for b in browsers if b["name"].startswith("chromium")}


def test_playwright_and_patchright_share_one_chromium_build():
    playwright = _revisions(_browsers_json("playwright"))
    patchright = _revisions(_browsers_json("patchright"))
    assert playwright["chromium"], playwright
    assert patchright == playwright, (
        f"patchright wants {patchright}, playwright installs {playwright}: "
        "pin versions that ship the same Chromium revision"
    )
