"""
One laptop carries several SKUs (plan step 11, decisions 15 and 16).

A page whose SKU is on no laptop is attached to the laptop of the same brand, product
line and specs, instead of becoming a second laptop: HP has a page per colour, ROG renames
a SKU and keeps the specs. Another product line, or one spec that differs, is a new
laptop. A laptop whose SKUs show different prices keeps the lowest, and the run says so.
The match itself is unit tested (tests/unit/test_sku_match.py); this is the processor.
"""
import pytest
from langchain_core.runnables import RunnableLambda
from sqlmodel import select

from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_models import Laptop, LaptopPriceHistory, LaptopSku
from app.processor import engine as processor
from app.processor.schemas import ExtractedLaptopFamily, ExtractedLaptopVariant
from app.scraper.models import RawScrapLaptop
from tests.integration.conftest import make_laptop

pytestmark = pytest.mark.integration

OMNIBOOK = "HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0090TU Silver"


def _hp_page(sku: str, colour: str, memory: str = "16 GB LPDDR5x-7467 MT/s (onboard)") -> dict:
    return {
        "Product Info / Sku": sku,
        "Appearance / Product color": colour,
        "Processors / Processors": "Intel® Core™ Ultra 5 322 (up to 4.4 GHz with Intel® Turbo Boost Technology)",
        "Display And Graphics / Graphics": "Intel® Graphics",
        "Memory / Memory": memory,
        "Storage / Hard drive description": "512 GB PCIe® Gen4 NVMe™ M.2 SSD",
        "Display And Graphics / Display": '40.6 cm (16") diagonal, 2K (1920 x 1200), IPS',
    }


def _hp_laptop(session, **over) -> Laptop:
    """An HP laptop as the catalog holds one today: specs, a price, no SKU."""
    brand = LaptopBrand(name="HP", base_scrape_url="https://example.invalid/hp")
    session.add(brand)
    session.commit()
    fields = dict(product_name=OMNIBOOK, model_code="hp-omnibook-5-16-ultra5-322-16gb-512gb",
                  processor_model="Intel Core Ultra 5 322", gpu_model="Intel Graphics", ram_gb=16, ssd_gb=512,
                  display_size_inch=16.0, price_rm=5099.0)
    laptop = make_laptop(brand.id, **{**fields, **over})
    session.add(laptop)
    session.commit()
    return laptop


def _process(session, monkeypatch, like: Laptop, name: str, features: dict, slug: str, price: float) -> dict:
    """One page through the processor. The stubbed LLM extracts one variant with the specs
    of *like* under *slug*, a wording no laptop has, so the slug never decides."""
    fields = {field: getattr(like, field, None) for field in ExtractedLaptopVariant.model_fields}
    fields.update(categories=[], unmapped_specs={"test": True}, model_code=slug, product_name=name, price_rm=price)
    extracted = ExtractedLaptopFamily.model_construct(variants=[ExtractedLaptopVariant.model_construct(**fields)])

    class _StubLLM:
        def with_structured_output(self, _schema):
            return RunnableLambda(lambda _: extracted)

    monkeypatch.setattr(processor, "build_llm", lambda *a, **k: _StubLLM())
    raw = RawScrapLaptop(source_url=f"https://example.invalid/{slug}", brand_id=like.brand_id, raw_product_name=name,
                         raw_specs_dump={"scraped_features": features})
    session.add(raw)
    session.commit()
    result = processor.process_raw_laptop_data(str(raw.id), session)
    assert result["status"] == "success", result
    return result


def _laptops(session, brand_id):
    return session.exec(select(Laptop).where(Laptop.brand_id == brand_id)).all()


def _skus(session, laptop_id):
    rows = session.exec(select(LaptopSku).where(LaptopSku.laptop_id == laptop_id)).all()
    return {row.sku: (row.colour, row.price_rm) for row in rows}


def test_an_hp_colour_page_attaches_to_the_existing_laptop(session, monkeypatch):
    laptop = _hp_laptop(session)

    result = _process(session, monkeypatch, laptop, "HP OmniBook 5 16 inch Laptop Next Gen AI 16-bm0089TU Blue",
                      _hp_page("E0GP3PA", "Starlit blue aluminum"), "hp-omnibook-5-blue-wording", 5099.0)

    assert [l.id for l in _laptops(session, laptop.brand_id)] == [laptop.id], "the colour page made a second laptop"
    assert _skus(session, laptop.id) == {"E0GP3PA": ("Starlit blue aluminum", 5099.0)}
    session.refresh(laptop)
    assert (laptop.model_code, laptop.manufacturer_sku) == ("hp-omnibook-5-16-ultra5-322-16gb-512gb", "E0GP3PA")
    assert result["skus_attached"] == [{
        "raw_id": result["writes"][0]["raw_id"], "laptop_id": str(laptop.id), "model_code": laptop.model_code,
        "sku": "E0GP3PA", "colour": "Starlit blue aluminum",
    }]
    assert result["writes"][0]["action"] == "updated"


def test_a_renamed_rog_sku_with_the_same_specs_attaches(session, brand, monkeypatch):
    # The catalog's own wording, trademark leftovers included
    laptop = make_laptop(brand.id, product_name="ROG Strix G16 (2025) G614 (G614P-WRV068W)",
                         model_code="asus-rog-strix-g16-ryzen9-8940hx-rtx5080-16gb-1tb",
                         processor_model="AMD Ryzen⁴ 9 8940HX Processor", gpu_model="NVIDIA® GeForce RTX⁴ 5080 Laptop GPU",
                         ram_gb=16, ssd_gb=1024, display_size_inch=16.0, price_rm=12999.0)
    laptop.manufacturer_sku = "G614P-WRV068W"
    session.add(laptop)
    session.commit()
    page = {
        "SKU": "G614P-WTS178W",
        "Processor": "AMD Ryzen™ 9 8940HX Processor 2.4GHz (80MB Cache, up to 5.3GHz, 16 cores)",
        "Graphics": "NVIDIA® GeForce RTX™ 5080 Laptop GPU, ROG Boost: 1700MHz* at 175W",
        "Memory": "16GB DDR5-5200 SO-DIMM",
        "Storage": "1TB PCIe® 4.0 NVMe™ M.2 SSD",
        "Display": "ROG Nebula Display, 16-inch, 2.5K (2560 x 1600, WQXGA) 16:10 aspect ratio",
    }

    _process(session, monkeypatch, laptop, "ROG Strix G16 (2025) G614 (G614P-WTS178W)", page, "rog-new-wording", 12999.0)

    assert [l.id for l in _laptops(session, brand.id)] == [laptop.id], "the renamed SKU made a second laptop"
    session.refresh(laptop)
    assert laptop.manufacturer_sku == "G614P-WRV068W", "the SKU the guides match on was replaced"
    assert _skus(session, laptop.id) == {"G614P-WTS178W": (None, 12999.0)}


def test_a_same_spec_page_of_another_product_line_is_a_new_laptop(session, monkeypatch):
    laptop = _hp_laptop(session, product_name="HP OmniBook 5 Flip 2-in-1 16 inch Laptop Next Gen AI 16-km0052TU Silver")

    result = _process(session, monkeypatch, laptop, "HP OmniBook X 16 inch Laptop Next Gen AI 16-ka0074TU Silver",
                      _hp_page("DP8F9PA", "Glacier silver aluminum"), "hp-omnibook-x-wording", 6999.0)

    rows = _laptops(session, laptop.brand_id)
    assert len(rows) == 2, "an OmniBook X page was attached to an OmniBook 5 Flip"
    new = next(l for l in rows if l.id != laptop.id)
    assert (new.manufacturer_sku, new.status, new.go_live_held) == ("DP8F9PA", "inactive", True)
    assert result["skus_attached"] == [] and _skus(session, laptop.id) == {}


def test_a_page_with_different_memory_is_a_new_laptop(session, monkeypatch):
    laptop = _hp_laptop(session)

    # The slug the LLM gives is the existing laptop's own: the specs decide, not the slug
    _process(session, monkeypatch, laptop, OMNIBOOK, _hp_page("E0GP9PA", "Sahara silver aluminum", "32 GB LPDDR5x (onboard)"),
             laptop.model_code, 5599.0)

    rows = _laptops(session, laptop.brand_id)
    assert len(rows) == 2, "a 32 GB page was attached to the 16 GB laptop"
    new = next(l for l in rows if l.id != laptop.id)
    assert new.model_code == f"{laptop.model_code}-e0gp9pa"
    session.refresh(laptop)
    assert laptop.price_rm == 5099.0


def test_the_lowest_price_of_a_laptops_skus_wins_and_the_difference_is_reported(session, monkeypatch):
    laptop = _hp_laptop(session, price_rm=5499.0)

    first = _process(session, monkeypatch, laptop, OMNIBOOK, _hp_page("E0GP5PA", "Sahara silver aluminum"), "w1", 5499.0)
    assert first["sku_price_differences"] == []

    second = _process(session, monkeypatch, laptop, OMNIBOOK.replace("90TU Silver", "89TU Blue"),
                      _hp_page("E0GP3PA", "Starlit blue aluminum"), "w2", 5099.0)
    session.refresh(laptop)
    assert laptop.price_rm == 5099.0
    assert second["sku_price_differences"] == [{
        "laptop_id": str(laptop.id), "model_code": laptop.model_code, "sku": "E0GP3PA", "sku_price": 5099.0,
        "kept_price": 5099.0, "other_skus": {"E0GP5PA": 5499.0},
    }]

    # The dearer page again, by its SKU this time: the laptop keeps the lower price
    third = _process(session, monkeypatch, laptop, OMNIBOOK, _hp_page("E0GP5PA", "Sahara silver aluminum"), "w3", 5499.0)
    session.refresh(laptop)
    assert laptop.price_rm == 5099.0, "the page processed last set the price"
    assert third["sku_price_differences"][0]["kept_price"] == 5099.0
    assert third["writes"] == [{"raw_id": third["writes"][0]["raw_id"], "laptop_id": str(laptop.id), "action": "updated"}]
    assert _skus(session, laptop.id) == {"E0GP5PA": ("Sahara silver aluminum", 5499.0),
                                         "E0GP3PA": ("Starlit blue aluminum", 5099.0)}
    history = session.exec(select(LaptopPriceHistory.price_rm).where(LaptopPriceHistory.laptop_id == laptop.id)).all()
    assert history == [5099.0], "one change, 5499 to 5099, is one history row"
