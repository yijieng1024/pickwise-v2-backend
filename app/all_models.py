"""
Every table model, imported in one place.

Importing this module registers every table on SQLModel.metadata. Anything that
needs the whole schema imports it: alembic/env.py, or `--autogenerate` reads a
table it cannot see as one to DROP; and every script run on its own (the
workflows' `python -m app.scripts...`), or the first flush fails resolving a
foreign key to a table nobody imported (`background_jobs.created_by` -> `users`).
The app never noticed: app.main imports every router, and so every model.

A new table module goes here, once. Importing any name from a module registers
all of that module's tables, so one import per module is enough.
"""
# ruff: noqa: F401
from app.laptops.laptop_models import Laptop, LaptopEmbedding, LaptopPriceHistory
from app.scraper.models import RawScrapLaptop
from app.laptops.brand_model import LaptopBrand
from app.laptops.family_model import LaptopFamily
from app.laptops.laptop_category_model import LaptopCategory
from app.taxonomy.product_type_model import ProductType
from app.taxonomy.category_model import Category
from app.users.models import User
from app.users.questionnaire_model import QuestionnaireQuestion
from app.scraper.models import ScrapeTarget
from app.scraper.raw_html_model import RawProductHtml
from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.rag.models import (
    Conversation,
    Message,
    ConversationLaptop,
    PipelineEvalLog,
)
from app.reviews.models import (
    YoutubeChannel,
    RawYoutubeReview,
    LaptopReviewChunk,
    LaptopReviewSummary,
)
from app.reviews.link_model import ReviewLaptopLink
from app.agent.monitoring_models import AgentRunLog
from app.saved.models import SavedLaptop
from app.common.job_model import BackgroundJob
from app.common.app_settings import AppSetting
from app.common.llm_usage import LLMUsageLog
from app.laptops.pickscore_general import LaptopPickScore
from app.laptops.price_guide_model import PriceGuide
from app.laptops.option_model import LaptopOption
from app.users.avatar_model import UserAvatar
from app.users.background_model import UserBackground
from app.pipeline.settings import PipelineSettings
