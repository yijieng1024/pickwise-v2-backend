# app/models/__init__.py

# We import the models here so that SQLModel's metadata registry 
# knows they exist before Alembic tries to generate migrations.

from .user import User
from .laptop import Laptop, LaptopBase, LaptopCreate, LaptopRead, LaptopEmbedding