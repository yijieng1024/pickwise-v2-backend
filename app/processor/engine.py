from datetime import datetime
import json
from typing import cast

from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from app.laptops.laptop_models import (
    RawScrapLaptop,
    Laptop,
)
from app.laptops.brand_model import LaptopBrand
from app.processor.schemas import ExtractedLaptopFamily

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
        model="gemini-3.5-flash",
        temperature=0,
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

                Extract all distinct laptop configurations found in this data.
                """,
            ),
        ]
    )

    chain = prompt_template | structured_llm

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
                }
            ),
        )

        saved_count = 0

        # 5. Map the AI output to your SQLModel (Laptop) and save to DB
        for variant in extracted_data.variants:
            if len(variant.unmapped_specs) > 0:
                spec_or_unmapped = variant.unmapped_specs
            else:
                spec_or_unmapped = {"ai_extraction_source": raw_data.raw_specs_dump}
            
            new_laptop = Laptop(
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
                microsoft_office_included=variant.microsoft_office_included,  # Note: renamed from microsoft_office
                bundled_accessories=variant.bundled_accessories,
                warranty_details=variant.warranty_details,

                # Part 9: External/Raw Assets
                raw_specs=spec_or_unmapped,
                image_urls=raw_data.image_urls
            )
            #spec_or_unmapped
            try:
                session.add(new_laptop)
                session.commit()
            except IntegrityError:
                session.rollback()
                print(f"⚠️ Skipped duplicate SKU: {variant.model_code}")

        raw_data.processing_status = "completed"

        session.add(raw_data)
        session.commit()

        return {
            "status": "success",
            "variants_extracted": len(extracted_data.variants),
            "variants_saved": saved_count,
        }

    except Exception as e:
        raw_data.processing_status = "failed"

        session.add(raw_data)
        session.commit()

        return {
            "status": "error",
            "message": str(e),
        }
