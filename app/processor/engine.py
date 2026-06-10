import json
from typing import cast

from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import ChatPromptTemplate

from app.laptops.models import (
    RawScrapLaptop,
    LaptopBrand,
    Laptop,
)
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
        model="gemini-3.1-flash-lite",
        temperature=0,
    )

    structured_llm = llm.with_structured_output(ExtractedLaptopFamily)

    system_prompt = """
        You are an expert hardware data engineer.

        Your task is to extract highly structured laptop specifications from
        scraped website text.

        Rules:

        1. The raw text may contain multiple different variants of the same
        product family (e.g. 14-inch and 16-inch models mixed together).

        2. Separate all variants into distinct configuration objects.

        3. Pay close attention to RAM, SSD, CPU, GPU, display size,
        refresh rate and pricing differences.

        4. Ignore marketing fluff, environmental reports, legal notices,
        warranty text and unrelated content.

        5. Deduce implicit Apple information:
        - os = "macOS"
        - gpu_brand = "Apple"
        - processor_brand = "Apple"
        - ai_ready = true

        6. Only extract information that can reasonably be inferred from
        the supplied data.

        7. If a field is unavailable, return null.
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

        for variant in extracted_data.variants:
            new_laptop = Laptop(
                brand_id=raw_data.brand_id,
                model_code=variant.model_code.lower(),
                product_name=variant.product_name,
                price_rm=variant.price_rm,
                cpu_benchmark=variant.cpu_benchmark,
                gpu_benchmark=variant.gpu_benchmark,
                ram_gb=variant.ram_gb,
                ssd_gb=variant.ssd_gb,
                weight_kg=variant.weight_kg,
                battery_wh=variant.battery_wh,
                display_size_inch=variant.display_size_inch,
                display_refresh_rate_hz=variant.display_refresh_rate_hz,
                release_year=variant.release_year,
                ai_ready=variant.ai_ready,
                microsoft_office=variant.microsoft_office,
                os=variant.os,
                gpu_brand=variant.gpu_brand,
                processor_brand=variant.processor_brand,
                raw_specs={"ai_extraction_source": raw_data.raw_specs_dump},
                image_urls=raw_data.image_urls,
            )

            try:
                session.add_all([new_laptop])
                session.commit()

                saved_count += 1

            except IntegrityError:
                session.rollback()

                print(f"⚠️ Skipped duplicate model_code: {variant.model_code}")

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
