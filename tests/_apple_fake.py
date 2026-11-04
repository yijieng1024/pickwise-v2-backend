"""A stand-in for apple.com in the step 10c tests, built from the real 2026-11-04 responses
of the MacBook Air 13 (M5 10-core CPU / 8-core GPU, 16GB, 512GB). No network.

The dependency-switcher answer for any requested option is the real 24GB answer, its
selected kit rebuilt as the current kit plus that option, plus the real rule: 24GB or
more, or 1TB or more, on the 13-inch moves the chip to 10-10."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

FIXTURES = Path(__file__).parent / "unit" / "fixtures" / "scraper"
AIR_BUY = "https://www.apple.com/my/shop/buy-mac/macbook-air"
AIR_SPECS = "https://www.apple.com/my/macbook-air/specs/"
AIR13_SLUG = "13-inch-silver-m5-chip-10-core-cpu-8-core-gpu-16gb-memory-512gb-storage"
# What each option adds to the RM 5,499 base, as the real calls priced them.
ADD_ONS = {"24gb": 900.0, "32gb": 1800.0, "1tb": 1350.0, "2tb": 3600.0, "4tb": 8100.0}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeApple:
    def __init__(self):
        self.calls: list[str] = []
        self.configure = fixture("apple_configure_air13.html")
        self.update_config = fixture("apple_update_config_air13_standard.json")
        self.switch = json.loads(fixture("apple_dependency_switcher_air13_24gb.json"))

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        parsed = urlparse(url)
        if parsed.path.endswith("/cto/update-config"):
            return self.update_config
        if parsed.path.endswith("/cto/dependency-switcher"):
            answer = copy.deepcopy(self.switch)
            overlay = answer["body"]["overlayContent"]
            dims = copy.deepcopy(overlay["currentKit"]["kitParts"]["dimensions"])
            for key, value in parse_qsl(parsed.query):
                if key.startswith("uv.") and key[3:] in dims:
                    dims[key[3:]]["dimensionValue"] = value
            if (dims["memory-dimensionMemory"]["dimensionValue"] != "16gb"
                    or dims["storage-dimensionCapacity"]["dimensionValue"] != "512gb"):
                dims["processor-cpuCoreCount-gpuCoreCount"]["dimensionValue"] = "10-10"
            overlay["selectedKit"]["kitParts"]["dimensions"] = dims
            overlay["currentKit"]["priceData"]["amount"] = 5499.0
            overlay["selectedKit"]["priceData"]["amount"] = 5499.0 + sum(
                ADD_ONS.get(value, 0.0) for key, value in parse_qsl(parsed.query) if key.startswith("uv."))
            return json.dumps(answer)
        if "/shop/buy-mac/" in parsed.path:
            return self.configure
        raise AssertionError(f"unexpected URL {url}")
