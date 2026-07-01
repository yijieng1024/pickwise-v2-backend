from fastmcp import FastMCP
from sqlmodel import Session, create_engine, select

from app.laptops.customization_model import LaptopCustomization
from app.laptops.laptop_models import Laptop
from app.config import settings

engine = create_engine(settings.database_url, echo=False)

mcp = FastMCP("pickwise-mcp")


@mcp.tool()
def calculate_custom_apple_price(
    model_code: str,
    selected_options: list[str],
) -> dict:
    """
    Calculate the total price for an Apple laptop with selected customization options.

    Looks up the laptop by model_code, retrieves the base price_rm, then sums the
    price_add_rm for each customization option whose option_name matches an entry in
    selected_options. Returns a full price breakdown.

    Args:
        model_code: The laptop's unique model code (e.g. "MNEH3ZP/A").
        selected_options: List of option names to add (e.g. ["32GB RAM", "1TB SSD"]).
    """
    with Session(engine) as session:
        laptop = session.exec(
            select(Laptop).where(Laptop.model_code == model_code)
        ).first()

        if not laptop:
            return {"error": f"Laptop with model_code '{model_code}' not found."}

        all_customizations = session.exec(
            select(LaptopCustomization).where(
                LaptopCustomization.laptop_id == laptop.id
            )
        ).all()

        selected_addons = [
            {
                "option_name": c.option_name,
                "category": c.category,
                "price_add_rm": c.price_add_rm,
            }
            for c in all_customizations
            if c.option_name in selected_options
        ]

        unrecognized = [
            opt for opt in selected_options
            if opt not in {c.option_name for c in all_customizations}
        ]

        total_addon_rm = sum(a["price_add_rm"] for a in selected_addons)

        return {
            "model_code": model_code,
            "product_name": laptop.product_name,
            "base_price_rm": laptop.price_rm,
            "selected_addons": selected_addons,
            "total_price_rm": laptop.price_rm + total_addon_rm,
            "unrecognized_options": unrecognized,
        }


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)
