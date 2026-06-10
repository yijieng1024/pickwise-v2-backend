from pydantic import BaseModel, Field
from typing import List, Optional

class ExtractedLaptopVariant(BaseModel):
    """Instructions for the AI to extract a single laptop configuration."""
    
    # We ask the AI to generate a unique slug for the model_code
    model_code: str = Field(description="Generate a unique, URL-friendly slug for this specific configuration. Format: brand-product-processor-ram-ssd. Example: 'apple-macbook-pro-14-m5-pro-16gb-1tb'")
    product_name: str = Field(description="Full variant name, e.g., '14-inch MacBook Pro (M5 Pro)'")
    price_rm: float = Field(description="Price in Malaysian Ringgit (RM). Extract only the number. If unknown, output 0.0")
    
    # Benchmarks (AI will estimate or default to 0)
    cpu_benchmark: int = Field(description="Estimate a PassMark CPU score based on the chip model (e.g., 15000 to 35000). Output 0 if unknown.")
    gpu_benchmark: int = Field(description="Estimate a PassMark GPU score. Output 0 if unknown.")
    
    ram_gb: int = Field(description="Total RAM in GB. Extract only the number.")
    ssd_gb: int = Field(description="Storage capacity in GB. Note: 1TB = 1024, 2TB = 2048.")
    weight_kg: float = Field(description="Weight in Kilograms (kg). If given in lbs, convert to kg. Default to 0.0 if missing.")
    battery_wh: int = Field(description="Battery capacity in Watt-hours (Wh). Output 0 if missing.")
    display_size_inch: float = Field(description="Screen size in inches. E.g., 14.2 or 16.0.")
    display_refresh_rate_hz: Optional[int] = Field(description="Screen refresh rate in Hz. E.g., 60 or 120. Output null if missing.")
    release_year: Optional[int] = Field(description="Release year. Default to current year if missing.")

    # AI-Enhanced Inferences
    ai_ready: bool = Field(description="True if specs mention 'Neural Engine', 'NPU', or 'AI capabilities'. False otherwise.")
    microsoft_office: bool = Field(description="True if Microsoft Office is explicitly mentioned as included. False otherwise.")
    os: Optional[str] = Field(description="Operating System. E.g., 'macOS', 'Windows 11'.")
    gpu_brand: Optional[str] = Field(description="GPU Brand. E.g., 'Apple', 'NVIDIA', 'AMD', 'Intel'.")
    processor_brand: Optional[str] = Field(description="Processor Brand. E.g., 'Apple', 'Intel', 'AMD'.")

class ExtractedLaptopFamily(BaseModel):
    """A wrapper to force the AI to return an array of variants."""
    variants: List[ExtractedLaptopVariant]