from logging.config import fileConfig
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from alembic import context

from sqlmodel import SQLModel
from app.laptops.models import Laptop, LaptopEmbedding
from app.users.models import User

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata