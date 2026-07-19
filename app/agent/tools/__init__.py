from app.agent.tools.laptop_tools import calculate_custom_apple_price, get_review_evidence
from app.agent.tools.market_price import search_malaysian_market_price
from app.agent.tools.search_laptops import make_search_laptops, search_laptops

ALL_TOOLS = [
    search_laptops,
    calculate_custom_apple_price,
    get_review_evidence,
    search_malaysian_market_price,
]

__all__ = [
    "search_laptops",
    "make_search_laptops",
    "calculate_custom_apple_price",
    "get_review_evidence",
    "search_malaysian_market_price",
    "ALL_TOOLS",
]
