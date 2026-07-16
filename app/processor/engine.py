from datetime import datetime
import json
import re
import time
from typing import List, Optional, cast

from sqlmodel import Session, select

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from app.config import settings
from app.laptops.laptop_models import Laptop, LaptopPriceHistory
from app.laptops.laptop_category_model import LaptopCategory
from app.scraper.models import RawScrapLaptop
from app.laptops.brand_model import LaptopBrand
from app.processor.schemas import ExtractedLaptopCategories, ExtractedLaptopFamily
from app.taxonomy.category_model import Category

_SIZE_TOKEN_RE = re.compile(r"(\d{2})[-_]inch")


def _filter_variant_images(
    image_urls: Optional[List[str]], display_size_inch: Optional[float]
) -> List[str]:
    """
    One raw scrape can cover a whole family (e.g. MacBook Air 13" + 15"), but
    each Laptop variant should only carry its own size's images. Keep URLs
    whose size token matches the variant's display_size_inch (13.6 → "13"),
    keep size-agnostic URLs, and drop /meta/…_og.png social-preview cards
    still present in older raw rows.
    """
    size_token = str(int(display_size_inch)) if display_size_inch else None
    filtered = []
    for url in image_urls or []:
        url_lower = url.lower()
        if "/meta/" in url_lower or "_og." in url_lower:
            continue
        sizes_in_url = _SIZE_TOKEN_RE.findall(url_lower)
        if sizes_in_url and size_token and size_token not in sizes_in_url:
            continue
        filtered.append(url)
    return filtered


def _resolve_category(
    session: Session, name: str, category_map: dict[str, Category]
) -> Optional[Category]:
    """
    Case-insensitive lookup against the categories already in the DB;
    creates the category when the model proposed a tag that doesn't exist
    yet. category_map is shared across all variants of a run so a tag
    created for variant 1 is reused (not duplicated) by variant 2.
    """
    cleaned = name.strip()
    if not cleaned:
        return None
    key = cleaned.lower()
    if key in category_map:
        return category_map[key]

    category = Category(name=cleaned)
    session.add(category)
    session.commit()
    session.refresh(category)
    category_map[key] = category
    return category


def _sync_laptop_categories(
    session: Session,
    laptop_id,
    tag_names: List[str],
    category_map: dict[str, Category],
) -> int:
    """
    Attach the model-chosen tags to the laptop via the laptop_categories
    junction. Additive on purpose: links added manually by admins are never
    removed when a raw row is re-processed.
    """
    existing_ids = {
        link.category_id
        for link in session.exec(
            select(LaptopCategory).where(LaptopCategory.laptop_id == laptop_id)
        ).all()
    }

    added = 0
    for name in tag_names:
        category = _resolve_category(session, name, category_map)
        if category is None or category.id in existing_ids:
            continue
        session.add(LaptopCategory(laptop_id=laptop_id, category_id=category.id))
        existing_ids.add(category.id)
        added += 1

    if added:
        session.commit()
    return added


def process_raw_laptop_data(
    raw_laptop_id: str,
    session: Session,
) -> dict:
    """
    Reads raw unstructured data, uses an LLM to parse it into strict
    Pydantic models, and saves the cleaned variants into the main
    Laptops table.
    """

    raw_data = session.exec(
        select(RawScrapLaptop).where(RawScrapLaptop.id == raw_laptop_id)
    ).first()

    if not raw_data:
        return {
            "status": "error",
            "message": "Raw laptop data not found",
        }

    brand = session.exec(
        select(LaptopBrand).where(LaptopBrand.id == raw_data.brand_id)
    ).first()

    brand_name = brand.name if brand else "Unknown"

    llm = ChatGoogleGenerativeAI(
        model="gemma-4-31b-it",
        temperature=0,
        google_api_key=settings.gemini_api_key,
    )

    structured_llm = llm.with_structured_output(ExtractedLaptopFamily)

    system_prompt = """
        You are an expert hardware data engineer.

        Your task is to extract highly structured laptop specifications from scraped website text into a list of flat, distinct SKU configurations (ExtractedLaptopVariant).

        CRITICAL RULES FOR GENERATING SKUS (Combinatorial Safety):
        1. Base Models First: Identify the distinct base processors listed (e.g., M5 Pro 14-core vs M5 Max 16-core). 
        2. Configurable Upgrades: The text will list "Configurable to" options (RAM/SSD) under each base model. You must generate a new, distinct SKU object for EVERY valid upgrade combination mentioned for that specific processor.
        3. ISOLATE THE PRICING MATRIX:
           - The raw text provides a sequential list of prices (e.g., RM 6,999, RM 7,849, RM 8,999).
           - Do NOT guess the price of upgraded SKUs by blindly assigning them numbers from this list.
           - ONLY assign a price to a SKU if the text explicitly pairs that price with a specific RAM/SSD configuration.
           - If you generate an upgraded SKU (e.g., a 48GB RAM variant) but the exact total price is NOT explicitly stated in the text, you MUST output 0.0 for `price_rm`. Do not hallucinate prices.
        4. INFERRING SKUS (model_code): Ensure the `model_code` you generate uniquely reflects the hardware.
           - Apple Format: apple-macbook-pro-[size]-m5-[tier]-[cpu]c-[gpu]g-[ram]gb-[ssd]gb. (e.g., apple-macbook-pro-14-m5-pro-14c-16g-24gb-1024gb).
           - PC Format: brand-product-cpumodel-gpumodel-ram-ssd.
        5. SDXC card slot at Macbook is a port to let creators use SD cards for media transfer, not for storage expansion. So it should not be considered as an expansion slot for storage.

        STANDARD EXTRACTION RULES:
        6. Separate all identified variants into distinct configuration objects.
        7. Pay close attention to RAM, SSD, CPU, GPU, display size, refresh rate.
        8. Deduce implicit Apple information if applicable:
           - os = "macOS"
           - gpu_brand = "Apple"
           - processor_brand = "Apple"
           - ai_ready = true
        9. Only extract information that can reasonably be inferred or calculated from the supplied data.
        10. Any leftover technical specifications, limitations, or legal footnotes MUST be collected and placed into the `unmapped_specs` JSON object.
        11. CATEGORY TAGS: For each variant, pick 1-3 use-case tags in `categories`, judged from the hardware
            (e.g. dedicated GPU + high refresh rate -> Gaming; light weight + long battery -> Business;
            strong media engine / color-accurate display -> Creator). You MUST reuse exact names from
            [AVAILABLE CATEGORIES] whenever one fits; only propose a new tag when none fit, and keep it a
            short Title Case use-case word — never a spec, chip, or brand name.
    """

    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            (
                "human",
                """
                Brand: {brand_name}

                Raw Product Name:
                {product_name}

                [Current System Year] 
                {current_year}

                [RAW PRICE MATRIX]
                {raw_prices}

                [RAW HARDWARE SPECS]
                {raw_specs}

                [AVAILABLE CATEGORIES]
                {available_categories}

                Extract all distinct laptop configurations found in this data.
                """,
            ),
        ]
    )

    chain = prompt_template | structured_llm

    # Category tagging: the model chooses from what's in the DB and may
    # propose new tags. The map includes inactive categories too, so a
    # deactivated tag is relinked rather than duplicated — but only active
    # ones are advertised in the prompt.
    all_categories = session.exec(select(Category)).all()
    category_map: dict[str, Category] = {
        c.name.strip().lower(): c for c in all_categories
    }
    available_categories = (
        ", ".join(sorted(c.name for c in all_categories if c.is_active))
        or "(none defined yet — propose suitable tags)"
    )

    try:
        extracted_data = cast(
            ExtractedLaptopFamily,
            chain.invoke(
                {
                    "brand_name": brand_name,
                    "product_name": raw_data.raw_product_name,
                    "current_year": datetime.now().year,
                    "raw_prices": json.dumps(
                        raw_data.raw_prices,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "raw_specs": json.dumps(
                        raw_data.raw_specs_dump,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "available_categories": available_categories,
                }
            ),
        )

        saved_count = 0
        updated_count = 0
        categories_linked = 0

        # 5. Map the AI output to your SQLModel (Laptop) and upsert to DB
        for variant in extracted_data.variants:
            spec_or_unmapped = variant.unmapped_specs if variant.unmapped_specs else {"ai_extraction_source": raw_data.raw_specs_dump}

            laptop_data = dict(
                # Part 1: Core Identifiers
                brand_id=raw_data.brand_id,
                model_code=variant.model_code.lower(),
                product_name=variant.product_name,
                release_year=variant.release_year,
                price_rm=variant.price_rm,

                # Part 2: Processor & AI Engine
                processor_brand=variant.processor_brand,
                processor_model=variant.processor_model,
                processor_ghz=variant.processor_ghz,
                cpu_cores=variant.cpu_cores,
                cpu_threads=variant.cpu_threads,
                npu_model=variant.npu_model,
                npu_tops=variant.npu_tops,
                ai_ready=variant.ai_ready,
                ai_features=variant.ai_features,

                # Part 3: Graphics & Hardware Acceleration
                gpu_brand=variant.gpu_brand,
                gpu_model=variant.gpu_model,
                gpu_cores=variant.gpu_cores,
                media_engine_details=variant.media_engine_details,

                # Part 4: Memory & Storage
                ram_gb=variant.ram_gb,
                ram_type=variant.ram_type,
                ram_upgradable=variant.ram_upgradable,
                max_ram_gb=variant.max_ram_gb,
                ssd_gb=variant.ssd_gb,
                storage_type=variant.storage_type,
                storage_upgradable=variant.storage_upgradable,
                expansion_slots_summary=variant.expansion_slots_summary,

                # Part 5: Display & External Video
                display_size_inch=variant.display_size_inch,
                display_resolution=variant.display_resolution,
                display_type=variant.display_type,
                display_refresh_rate_hz=variant.display_refresh_rate_hz,
                display_brightness_nits=variant.display_brightness_nits,
                touchscreen=variant.touchscreen,
                external_display_support=variant.external_display_support,

                # Part 6: Build, Battery & Connectivity
                weight_kg=variant.weight_kg,
                dimensions_cm=variant.dimensions_cm,
                battery_wh=variant.battery_wh,
                power_supply_details=variant.power_supply_details,
                os=variant.os,
                colors=variant.colors,
                ports_summary=variant.ports_summary,
                wifi_standard=variant.wifi_standard,
                bluetooth_version=variant.bluetooth_version,

                # Part 7: Peripherals, Input & Audio
                keyboard_touchpad_details=variant.keyboard_touchpad_details,
                audio_details=variant.audio_details,
                camera_details=variant.camera_details,
                facial_recognition=variant.facial_recognition,
                fingerprint_reader=variant.fingerprint_reader,

                # Part 8: Security, Certifications & Extras
                security_features=variant.security_features,
                materials_and_certifications=variant.materials_and_certifications,
                microsoft_office_included=variant.microsoft_office_included,
                bundled_accessories=variant.bundled_accessories,
                warranty_details=variant.warranty_details,

                # Part 9: External/Raw Assets
                raw_specs=spec_or_unmapped,
                image_urls=_filter_variant_images(
                    raw_data.image_urls, variant.display_size_inch
                ),
            )

            existing = session.exec(
                select(Laptop).where(Laptop.model_code == variant.model_code.lower())
            ).first()

            if existing:
                # Price history: only record if price actually changed and is non-zero
                price_changed = variant.price_rm > 0 and variant.price_rm != existing.price_rm

                for key, value in laptop_data.items():
                    if key != "model_code":  # model_code is the lookup key — never overwrite
                        setattr(existing, key, value)

                session.add(existing)
                session.commit()

                if price_changed:
                    session.add(LaptopPriceHistory(laptop_id=existing.id, price_rm=variant.price_rm))
                    session.commit()

                categories_linked += _sync_laptop_categories(
                    session, existing.id, variant.categories, category_map
                )
                updated_count += 1
            else:
                new_laptop = Laptop(**laptop_data)
                session.add(new_laptop)
                session.commit()
                session.refresh(new_laptop)

                if new_laptop.price_rm > 0:
                    session.add(LaptopPriceHistory(laptop_id=new_laptop.id, price_rm=new_laptop.price_rm))
                    session.commit()

                categories_linked += _sync_laptop_categories(
                    session, new_laptop.id, variant.categories, category_map
                )
                saved_count += 1

        raw_data.processing_status = "completed"

        session.add(raw_data)
        session.commit()

        return {
            "status": "success",
            "variants_extracted": len(extracted_data.variants),
            "variants_saved": saved_count,
            "variants_updated": updated_count,
            "categories_linked": categories_linked,
        }

    except Exception as e:
        raw_data.processing_status = "failed"

        session.add(raw_data)
        session.commit()

        return {
            "status": "error",
            "message": str(e),
        }


_CATEGORIZE_SYSTEM_PROMPT = """
    You are an expert laptop product analyst.

    You will be given the specifications of ONE laptop already in our catalog.
    Pick 1-3 use-case tags in `categories`, judged from the hardware
    (e.g. dedicated GPU + high refresh rate -> Gaming; light weight + long battery -> Business;
    strong media engine / color-accurate display -> Creator). You MUST reuse exact names from
    [AVAILABLE CATEGORIES] whenever one fits; only propose a new tag when none fit, and keep it a
    short Title Case use-case word — never a spec, chip, or brand name.
"""

# Same Gemma free-tier pacing as the router's bulk processing: 15 RPM -> 5 s gap.
_CATEGORIZE_DELAY_S = 5


def categorize_untagged_laptops(session: Session, limit: int = 100) -> dict:
    """
    Backfill step for laptops processed before category tagging existed (or
    whose tags were removed): finds laptops with no laptop_categories link
    and asks the LLM to tag each one from its stored specs — no raw scrape
    row needed. Tags missing from the DB are created, exactly like the main
    processor flow. Safe to re-run: already-tagged laptops are never selected.
    """
    # Deferred import to avoid constructing the embeddings client (and its
    # OpenRouter config) whenever the processor module is merely imported.
    from app.embeddings.service import build_laptop_embedding_text

    untagged = session.exec(
        select(Laptop)
        .where(Laptop.id.not_in(select(LaptopCategory.laptop_id)))  # type: ignore[union-attr]
        .limit(limit)
    ).all()

    if not untagged:
        return {
            "status": "success",
            "message": "No untagged laptops found.",
            "attempted": 0,
            "tagged": 0,
            "links_added": 0,
            "untagged_remaining": 0,
            "errors": [],
        }

    brands = session.exec(select(LaptopBrand)).all()
    brand_map = {b.id: b.name for b in brands}

    all_categories = session.exec(select(Category)).all()
    category_map: dict[str, Category] = {
        c.name.strip().lower(): c for c in all_categories
    }

    llm = ChatGoogleGenerativeAI(
        model="gemma-4-31b-it",
        temperature=0,
        google_api_key=settings.gemini_api_key,
    )
    structured_llm = llm.with_structured_output(ExtractedLaptopCategories)
    prompt_template = ChatPromptTemplate.from_messages(
        [
            ("system", _CATEGORIZE_SYSTEM_PROMPT),
            (
                "human",
                """
                [AVAILABLE CATEGORIES]
                {available_categories}

                [LAPTOP SPECS]
                {laptop_specs}

                Pick the category tags for this laptop.
                """,
            ),
        ]
    )
    chain = prompt_template | structured_llm

    tagged = 0
    links_added = 0
    errors: list[dict] = []

    for i, laptop in enumerate(untagged):
        if i > 0:
            time.sleep(_CATEGORIZE_DELAY_S)
        try:
            # Rebuilt each iteration: a tag created for laptop 1 is offered
            # to laptop 2 instead of being re-proposed under a variant name.
            available_categories = (
                ", ".join(
                    sorted(c.name for c in category_map.values() if c.is_active)
                )
                or "(none defined yet — propose suitable tags)"
            )
            spec_text = build_laptop_embedding_text(
                laptop, brand_map.get(laptop.brand_id, "Unknown")
            )
            result = cast(
                ExtractedLaptopCategories,
                chain.invoke(
                    {
                        "available_categories": available_categories,
                        "laptop_specs": spec_text,
                    }
                ),
            )
            added = _sync_laptop_categories(
                session, laptop.id, result.categories, category_map
            )
            links_added += added
            if added:
                tagged += 1
        except Exception as e:
            errors.append(
                {
                    "laptop_id": str(laptop.id),
                    "product_name": laptop.product_name,
                    "error": str(e),
                }
            )

    untagged_remaining = len(
        session.exec(
            select(Laptop.id).where(
                Laptop.id.not_in(select(LaptopCategory.laptop_id))  # type: ignore[union-attr]
            )
        ).all()
    )

    return {
        "status": "success",
        "attempted": len(untagged),
        "tagged": tagged,
        "links_added": links_added,
        "untagged_remaining": untagged_remaining,
        "errors": errors,
    }
