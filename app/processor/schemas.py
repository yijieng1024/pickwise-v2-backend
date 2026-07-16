from pydantic import BaseModel, Field
from typing import Dict, List, Optional

class ExtractedLaptopVariant(BaseModel):
    """Instructions for the AI to extract a single laptop configuration (Flat SKU Row)."""
    
    # Part 1: Core Identifiers & Categorization
    model_code: str = Field(
        description=(
            "Generate a unique, URL-friendly SKU code for this exact configuration. "
            "CRITICAL NAMING RULES: "
            "1. For Apple: MUST include processor tier and specific core counts if multiple exist. Format: apple-product-processor-[cpu]c-[gpu]g-ram-ssd. (e.g., apple-macbook-pro-14-m5-pro-15c-16g-16gb-512gb). "
            "2. For PC (Windows): MUST include specific CPU model and GPU model. Format: brand-product-cpumodel-gpumodel-ram-ssd. (e.g., asus-rog-zephyrus-g14-ryzen9-7940hs-rtx4070-32gb-1tb). "
        )
    )
    product_name: str = Field(description="Full variant name, e.g., '14-inch MacBook Pro (M5 Pro, 16GB RAM, 512GB SSD)'")
    categories: List[str] = Field(
        default_factory=list,
        description=(
            "1-3 use-case tags describing who this laptop is for, judged from its specs "
            "(e.g. a dedicated RTX GPU + high refresh rate → 'Gaming'; light weight + long "
            "battery → 'Business'). STRONGLY prefer exact names from the [AVAILABLE CATEGORIES] "
            "list in the prompt. Only invent a new tag when none of the available ones fit; a new "
            "tag must be a short Title Case use-case word (like 'Workstation'), never a spec, "
            "chip, or brand name."
        ),
    )
    release_year: Optional[int] = Field(description="Estimate the release year based on the processor generation. If completely unknown, use the 'Current System Year' provided in the prompt.")
    price_rm: float = Field(description="Price in Malaysian Ringgit (RM). Extract only the number. If unknown, output 0.0")

    # Part 2: Processor & AI Engine
    processor_brand: Optional[str] = Field(description="Processor Brand. E.g., 'Apple', 'Intel', 'AMD', 'Qualcomm'.")
    processor_model: str = Field(description="The exact CPU model. For Apple: e.g., 'Apple M5 Pro (15-core)'. For PC: e.g., 'Intel Core Ultra 5 225H'.")
    processor_ghz: Optional[str] = Field(description="Base or boost clock speed if mentioned, e.g., '1.7 GHz up to 4.9 GHz'. Output null if missing.")
    cpu_cores: Optional[int] = Field(description="Total number of CPU cores. Extract only the integer.")
    cpu_threads: Optional[int] = Field(description="Total number of CPU threads. Extract only the integer.")
    npu_model: Optional[str] = Field(description="Name of the Neural Processing Unit if present, e.g., 'Intel AI Boost NPU' or '16-core Neural Engine'.")
    npu_tops: Optional[float] = Field(description="The TOPS (Tera Operations Per Second) rating of the NPU. Extract only the number. Output null if missing.")
    ai_ready: bool = Field(description="True if specs mention 'Neural Engine', 'NPU', 'Copilot+ PC', or 'AI capabilities'. False otherwise.")
    ai_features: List[str] = Field(default_factory=list, description="List of specific AI software/branding features, e.g., ['Copilot+ PC', 'Apple Intelligence'].")

    # Part 3: Graphics & Hardware Acceleration
    gpu_brand: Optional[str] = Field(description="GPU Brand. E.g., 'Apple', 'NVIDIA', 'AMD', 'Intel'.")
    gpu_model: str = Field(description="The exact GPU model. For Apple: e.g., '16-core GPU'. For PC: e.g., 'Intel Arc Graphics'.")
    gpu_cores: Optional[int] = Field(description="Total number of GPU cores (especially important for Apple Silicon). Extract only the integer.")
    media_engine_details: Optional[str] = Field(description="Details on video encoding/decoding engines, e.g., 'Hardware-accelerated ProRes, HEVC, AV1 decode'.")

    # Part 4: Memory & Storage
    ram_gb: int = Field(description="Total RAM in GB. Extract only the integer.")
    ram_type: Optional[str] = Field(description="Type of RAM, e.g., 'DDR5', 'LPDDR5X', 'Unified Memory'.")
    ram_upgradable: bool = Field(description="True if RAM is described as SO-DIMM or upgradable. False if described as 'on board', 'soldered', or 'unified memory'.")
    max_ram_gb: Optional[int] = Field(description="Maximum supported RAM in GB if mentioned. Extract only the integer.")
    
    ssd_gb: int = Field(description="Storage capacity in GB. Note: 1TB = 1024, 2TB = 2048. Extract only the integer.")
    storage_type: Optional[str] = Field(description="Type of storage, e.g., 'M.2 NVMe PCIe 4.0 SSD'.")
    storage_upgradable: bool = Field(description="True if storage is described with available M.2 slots. False if soldered or unified.")
    expansion_slots_summary: Optional[str] = Field(description="Summarize internal expansion slots, e.g., '1x M.2 2280 PCIe 4.0x4, 1x DDR5 SO-DIMM slot'.")

    # Part 5: Display & External Video
    display_size_inch: float = Field(description="Screen size in inches. Extract only the number, e.g., 14.2.")
    display_resolution: Optional[str] = Field(description="Screen resolution, e.g., '1920 x 1200' or '3024x1964'.")
    display_type: Optional[str] = Field(description="Panel technology, e.g., 'IPS-level Panel', 'Liquid Retina XDR', 'OLED'.")
    display_refresh_rate_hz: Optional[int] = Field(description="Screen refresh rate in Hz. Extract only the integer, e.g., 120.")
    display_brightness_nits: Optional[int] = Field(description="Peak or SDR brightness in nits. Extract only the integer.")
    touchscreen: bool = Field(description="True if the screen supports touch input. False otherwise.")
    external_display_support: Optional[str] = Field(description="Summarize maximum external monitors supported and their resolutions/refresh rates.")

    # Part 6: Build, Battery & Connectivity
    weight_kg: float = Field(description="Weight in Kilograms (kg). If given in lbs, convert to kg. Default to 0.0 if missing.")
    dimensions_cm: Optional[str] = Field(description="Dimensions (Width x Depth x Height) in cm. Convert from inches if necessary.")
    battery_wh: float = Field(description="Battery capacity in Watt-hours (Wh). Output a float. Output 0.0 if missing.")
    power_supply_details: Optional[str] = Field(description="Details of the charger, e.g., '70W USB-C Power Adapter', 'MagSafe 3'.")
    os: Optional[str] = Field(description="Operating System. E.g., 'macOS', 'Windows 11 Home'.")
    colors: List[str] = Field(default_factory=list, description="List of available color options, e.g., ['Space Black', 'Silver']. in Apple case is section Finish")
    ports_summary: List[str] = Field(default_factory=list, description="List of physical I/O ports, e.g., ['2x USB 3.2 Gen 1 Type-A', '1x HDMI 1.4'].")
    wifi_standard: Optional[str] = Field(description="Wi-Fi generation, e.g., 'Wi-Fi 6E', 'Wi-Fi 7'.")
    bluetooth_version: Optional[str] = Field(description="Bluetooth version, e.g., '5.3', '6.0'.")

    # Part 7: Peripherals, Input & Audio
    keyboard_touchpad_details: Optional[str] = Field(description="Details on keyboard and trackpad, e.g., 'Backlit Magic Keyboard, Force Touch trackpad', 'With Copilot key'.")
    audio_details: Optional[str] = Field(description="Speaker and microphone details, e.g., 'Six-speaker sound system with Spatial Audio', 'Built-in array microphone'.")
    camera_details: Optional[str] = Field(description="Webcam specs, e.g., '12MP Center Stage', 'FHD camera with privacy shutter'.")
    facial_recognition: bool = Field(description="True if Windows Hello IR camera or FaceID is explicitly supported.")
    fingerprint_reader: bool = Field(description="True if Touch ID or a fingerprint reader is explicitly supported.")

    # Part 8: Security, Certifications & Extras
    security_features: Optional[str] = Field(description="Hardware/Software security features, e.g., 'Firmware TPM, BIOS Booting Password'.")
    materials_and_certifications: Optional[str] = Field(description="Build materials or certifications, e.g., 'US MIL-STD 810H military-grade', 'ENERGY STAR'.")
    microsoft_office_included: bool = Field(description="True if Microsoft Office or Microsoft 365 is explicitly mentioned as included. False otherwise.")
    bundled_accessories: Optional[str] = Field(description="Items included in the box besides the laptop and charger, e.g., 'Backpack', 'Polishing cloth'.")
    warranty_details: Optional[str] = Field(description="Details on warranty and support, e.g., '1 Year Limited Warranty'.")

    # Part 9: The "Catch-All" for Leftovers
    unmapped_specs: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extract any specifications, footnotes, software trials, or legal disclaimers "
            "from the source text that DO NOT fit into any of the explicitly defined fields above. "
            "Store them as Key-Value pairs (e.g., {'xbox_game_pass': '3 months included', 'm2_slot_restriction': 'only single-sided SSDs supported'}). "
            "DO NOT duplicate data already extracted in other fields."
        )
    )

class ExtractedLaptopFamily(BaseModel):
    variants: List[ExtractedLaptopVariant]


class ExtractedLaptopCategories(BaseModel):
    """Instructions for the AI to tag one existing laptop with use-case categories."""

    categories: List[str] = Field(
        description=(
            "1-3 use-case tags describing who this laptop is for, judged from its specs "
            "(e.g. a dedicated RTX GPU + high refresh rate → 'Gaming'; light weight + long "
            "battery → 'Business'). STRONGLY prefer exact names from the [AVAILABLE CATEGORIES] "
            "list in the prompt. Only invent a new tag when none of the available ones fit; a new "
            "tag must be a short Title Case use-case word (like 'Workstation'), never a spec, "
            "chip, or brand name."
        ),
    )