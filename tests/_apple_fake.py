"""A stand-in for apple.com in the step 10c tests, built from real 2026-11-04 responses
of the MacBook Air 13 (M5). No network.

- Configure pages: the 10-8 base's (16GB / 512GB); a 10-10 slug gets the same page with
  its kit set to the 10-10 standard config (16GB / 1TB).
- update-config: the real answer for the kit's chip: the 10-8 base (RM 5,499, only
  16GB and 512GB) or the 10-10 / 1TB standard config (RM 6,849, 16-32GB, 512GB-4TB).
- dependency-switcher: the real 24GB answer, its selected kit rebuilt as the current kit
  plus the requested change."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

FIXTURES = Path(__file__).parent / "unit" / "fixtures" / "scraper"
AIR_BUY = "https://www.apple.com/my/shop/buy-mac/macbook-air"
AIR_SPECS = "https://www.apple.com/my/macbook-air/specs/"
AIR13_SLUG = "13-inch-silver-m5-chip-10-core-cpu-8-core-gpu-16gb-memory-512gb-storage"
AIR13_1010_SLUG = "13-inch-silver-m5-chip-10-core-cpu-10-core-gpu-16gb-memory-1tb-storage"
CHIP = "processor-cpuCoreCount-gpuCoreCount"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeApple:
    def __init__(self):
        self.calls: list[str] = []
        self.configure = fixture("apple_configure_air13.html")
        self.update_config = {"10-8": fixture("apple_update_config_air13_standard.json"),
                              "10-10": fixture("apple_update_config_air13_1tb.json")}
        self.switch = json.loads(fixture("apple_dependency_switcher_air13_24gb.json"))

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        parsed = urlparse(url)
        params = parse_qsl(parsed.query)
        if parsed.path.endswith("/cto/update-config"):
            return self.update_config[dict(params)[f"sv.{CHIP}"]]
        if parsed.path.endswith("/cto/dependency-switcher"):
            answer = copy.deepcopy(self.switch)
            overlay = answer["body"]["overlayContent"]
            dims = copy.deepcopy(overlay["currentKit"]["kitParts"]["dimensions"])
            for key, value in params:
                if key.startswith("uv.") and key[3:] in dims:
                    dims[key[3:]]["dimensionValue"] = value
            overlay["selectedKit"]["kitParts"]["dimensions"] = dims
            return json.dumps(answer)
        if "/shop/buy-mac/" in parsed.path:
            if "10-core-cpu-10-core-gpu" in parsed.path:
                return self.configure.replace(f'"{CHIP}": "10-8"', f'"{CHIP}": "10-10"').replace(
                    '"storage-dimensionCapacity": "512gb"', '"storage-dimensionCapacity": "1tb"')
            return self.configure
        raise AssertionError(f"unexpected URL {url}")


EDU_PRICES = {AIR13_SLUG: ("MDH74ZP/A", 5099.0), AIR13_1010_SLUG: ("MDHA4ZP/A", 6449.0)}
EDU_KIT_PRICE = {"10-8": 5099.0, "10-10": 6449.0}


class FakeAppleEdu(FakeApple):
    """Apple's education store (step 10d): refuses anything not under /my-edu/. A
    configuration page also states its part number and Edu price (the Air 13 base's for
    any slug it does not know), and each kit is RM 400 cheaper than on the main store."""

    def __init__(self):
        super().__init__()
        for chip, price in EDU_KIT_PRICE.items():
            payload = json.loads(self.update_config[chip])
            payload["body"]["selectedKits"]["priceData"]["amount"] = price
            self.update_config[chip] = json.dumps(payload)

    def __call__(self, url: str) -> str:
        assert "apple.com/my-edu/" in url, f"not the education store: {url}"
        body = super().__call__(url)
        if "/shop/buy-mac/" in url and "/cto/" not in url:
            part, price = EDU_PRICES.get(url.rsplit("/", 1)[1], EDU_PRICES[AIR13_SLUG])
            body += ('"products":[{"sku":"X","partNumber":"%s","price":{"fullPrice":%.2f},'
                     '"category":"mac","name":"MacBook Air"}]' % (part, price))
        return body
