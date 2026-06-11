from datetime import datetime

from pydantic import BaseModel, Field
from typing import List, Optional

def get_current_year():
    return datetime.now().year

class ExtractedLaptopVariant(BaseModel):
    """Instructions for the AI to extract a single laptop configuration (Flat SKU Row)."""
    
    # model_code is now your official SKU identifier
    model_code: str = Field(
        description=(
            "Generate a unique, URL-friendly SKU code for this exact configuration. "
            "CRITICAL NAMING RULES: "
            "1. For Apple: MUST include processor tier and specific core counts if multiple exist. Format: apple-product-processor-[cpu]c-[gpu]g-ram-ssd. (e.g., apple-macbook-pro-14-m5-pro-15c-16g-16gb-512gb). "
            "2. For PC (Windows): MUST include specific CPU model and GPU model. Format: brand-product-cpumodel-gpumodel-ram-ssd. (e.g., asus-rog-zephyrus-g14-ryzen9-7940hs-rtx4070-32gb-1tb). "
        )
    )
    product_name: str = Field(description="Full variant name, e.g., '14-inch MacBook Pro (M5 Pro, 16GB RAM, 512GB SSD)'")
    
    # Processor and GPU exact names (needed later for your benchmark joins)
    processor_model: str = Field(description="The exact CPU model. For Apple: e.g., 'Apple M5 Pro (15-core)'. For PC: e.g., 'Intel Core i7-13700H' or 'AMD Ryzen 9 7945HX'.")
    gpu_model: str = Field(description="The exact GPU model. For Apple: e.g., '16-core GPU'. For PC: e.g., 'Nvidia GeForce RTX 4070 8GB'. If TGP is mentioned, include it.")
    
    # 💡 REMOVED: cpu_benchmark and gpu_benchmark
    
    price_rm: float = Field(description="Price in Malaysian Ringgit (RM). Extract only the number. If unknown, output 0.0")
    
    ram_gb: int = Field(description="Total RAM in GB. Extract only the number.")
    ssd_gb: int = Field(description="Storage capacity in GB. Note: 1TB = 1024, 2TB = 2048.")
    weight_kg: float = Field(description="Weight in Kilograms (kg). If given in lbs, convert to kg. Default to 0.0 if missing.")
    battery_wh: int = Field(description="Battery capacity in Watt-hours (Wh). Output 0 if missing.")
    display_size_inch: float = Field(description="Screen size in inches. E.g., 14.2 or 16.0.")
    display_refresh_rate_hz: Optional[int] = Field(description="Screen refresh rate in Hz. E.g., 60 or 120. Output null if missing.")
    
    # We still keep the dynamic release year instruction
    release_year: Optional[int] = Field(description="Estimate the release year based on the processor generation. If completely unknown, use the 'Current System Year' provided in the prompt.")

    # AI-Enhanced Inferences
    ai_ready: bool = Field(description="True if specs mention 'Neural Engine', 'NPU', or 'AI capabilities'. False otherwise.")
    microsoft_office: bool = Field(description="True if Microsoft Office is explicitly mentioned as included. False otherwise.")
    os: Optional[str] = Field(description="Operating System. E.g., 'macOS', 'Windows 11'.")
    gpu_brand: Optional[str] = Field(description="GPU Brand. E.g., 'Apple', 'NVIDIA', 'AMD', 'Intel'.")
    processor_brand: Optional[str] = Field(description="Processor Brand. E.g., 'Apple', 'Intel', 'AMD'.")

class ExtractedLaptopFamily(BaseModel):
    variants: List[ExtractedLaptopVariant]