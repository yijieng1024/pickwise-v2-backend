import os
from dotenv import load_dotenv
from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

from sqlmodel import SQLModel
from app.laptops.laptop_models import Laptop, LaptopEmbedding, LaptopPriceHistory
from app.scraper.models import RawScrapLaptop
from app.laptops.customization_model import LaptopCustomization
from app.laptops.brand_model import LaptopBrand
from app.laptops.laptop_category_model import LaptopCategory
from app.taxonomy.product_type_model import ProductType
from app.taxonomy.category_model import Category
from app.users.models import User
from app.users.questionnaire_model import QuestionnaireQuestion
from app.scraper.models import ScrapeTarget
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
from app.agent.monitoring_models import AgentRunLog

load_dotenv()

config = context.config

db_url = os.environ.get("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
