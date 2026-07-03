"""add taxonomy tables, questionnaire, budget range, customization category fk

Revision ID: ffb4429867dd
Revises: fe5716d7dbf4
Create Date: 2026-07-04 00:43:09.560538

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ffb4429867dd'
down_revision: Union[str, Sequence[str], None] = 'fe5716d7dbf4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# -- seed data -----------------------------------------------------------
# Matches the PickWise v1 6-step questionnaire; `value` is exactly what a
# frontend submission would send to the existing PUT /me/preferences.
_QUESTIONNAIRE_SEED = [
    {
        "step_order": 1,
        "question_text": "What is your budget range for the laptop/desktop?",
        "question_type": "SINGLE_CHOICE",
        "target_field": "budget",
        "options": [
            {"value": {"min": None, "max": 2000}, "label": "< RM 2000"},
            {"value": {"min": 2000, "max": 3000}, "label": "RM 2000 – RM 3000"},
            {"value": {"min": 3000, "max": 5000}, "label": "RM 3000 – RM 5000"},
            {"value": {"min": 5000, "max": None}, "label": "> RM 5000"},
        ],
        "help_text": "To know user's budget range as reference",
    },
    {
        "step_order": 2,
        "question_text": "What will you mainly use the laptop/desktop for?",
        "question_type": "SINGLE_CHOICE",
        "target_field": "purpose",
        "options": [
            {"value": "Office/Study", "label": "Office / Study (basic tasks)"},
            {"value": "Programming/Development", "label": "Programming / Development"},
            {"value": "Gaming", "label": "Gaming"},
            {"value": "Creative Work", "label": "Creative Work (Design, Video Editing, 3D, etc.)"},
            {"value": "General Use", "label": "General Use (Mixed / Casual)"},
        ],
        "help_text": "To know user's usage of laptop as reference",
    },
    {
        "step_order": 3,
        "question_text": (
            "What is the most important factor(s) when choosing a laptop/desktop? "
            "Please drag the below card for priority ranking"
        ),
        "question_type": "RANKING",
        "target_field": "priorities",
        "options": [
            {"value": "price", "label": "Price"},
            {"value": "cpu", "label": "CPU Performance"},
            {"value": "gpu", "label": "GPU Performance"},
            {"value": "portability", "label": "Portability (weight, size)"},
            {"value": "battery", "label": "Battery Life"},
            {"value": "brand", "label": "Brand / Reliability"},
        ],
        "help_text": "To know user's priority factor that may affect choosing the laptop",
    },
    {
        "step_order": 4,
        "question_text": "What screen size do you prefer?",
        "question_type": "SINGLE_CHOICE",
        "target_field": "screen_size",
        "options": [
            {"value": "13-14", "label": "13\" – 14\" (Compact)"},
            {"value": "15-16", "label": "15\" – 16\" (Balanced)"},
            {"value": "17+", "label": "17\" and above (Large Display)"},
        ],
        "help_text": "To know what screen size user prefers as reference",
    },
    {
        "step_order": 5,
        "question_text": "Do you value portability (thin & light design)?",
        "question_type": "SINGLE_CHOICE",
        "target_field": "portability",
        "options": [
            {"value": "Yes", "label": "Yes, I need a light device"},
            {"value": "Neutral", "label": "Neutral, doesn't matter"},
            {"value": "No", "label": "No, performance is more important"},
        ],
        "help_text": "To know if user prefers a light or heavy laptop",
    },
    {
        "step_order": 6,
        "question_text": "Do you have a preferred brand?",
        "question_type": "SINGLE_CHOICE",
        "target_field": "brand_preferences",
        "options": None,  # sourced dynamically from GET /brands
        "help_text": "To know user's preferred brand — options come from GET /brands",
    },
]


def upgrade() -> None:
    """Upgrade schema."""
    now = datetime.now(timezone.utc)

    # -- 1. New reference/catalog tables ----------------------------------
    op.create_table('categories',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('icon_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_categories_name'), 'categories', ['name'], unique=True)

    op.create_table('product_types',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_product_types_name'), 'product_types', ['name'], unique=True)

    op.create_table('questionnaire_questions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('product_type_id', sa.Uuid(), nullable=False),
        sa.Column('step_order', sa.Integer(), nullable=False),
        sa.Column('question_text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('question_type', sa.Enum('SINGLE_CHOICE', 'RANKING', name='questiontype'), nullable=False),
        sa.Column('target_field', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('options', sa.JSON(), nullable=True),
        sa.Column('help_text', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_type_id'], ['product_types.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_questionnaire_questions_product_type_id'), 'questionnaire_questions', ['product_type_id'], unique=False)
    op.create_index(op.f('ix_questionnaire_questions_step_order'), 'questionnaire_questions', ['step_order'], unique=False)

    op.create_table('laptop_categories',
        sa.Column('laptop_id', sa.Uuid(), nullable=False),
        sa.Column('category_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ),
        sa.ForeignKeyConstraint(['laptop_id'], ['laptops.id'], ),
        sa.PrimaryKeyConstraint('laptop_id', 'category_id'),
    )

    connection = op.get_bind()

    # -- 2. Backfill categories from existing laptop_customizations.category
    #    string values (data-preserving — every existing row already has a
    #    NOT NULL category string per the init migration).
    categories_table = sa.table(
        'categories',
        sa.column('id', sa.Uuid()),
        sa.column('name', sa.String()),
        sa.column('is_active', sa.Boolean()),
        sa.column('created_at', sa.DateTime()),
    )
    distinct_categories = connection.execute(
        sa.text("SELECT DISTINCT category FROM laptop_customizations WHERE category IS NOT NULL")
    ).fetchall()
    if distinct_categories:
        op.bulk_insert(categories_table, [
            {"id": uuid.uuid4(), "name": row[0], "is_active": True, "created_at": now}
            for row in distinct_categories
        ])

    # -- 3. laptop_customizations: add category_id nullable, backfill by
    #    matching name, THEN enforce NOT NULL and drop the old string column.
    op.add_column('laptop_customizations', sa.Column('category_id', sa.Uuid(), nullable=True))
    connection.execute(sa.text(
        "UPDATE laptop_customizations lc SET category_id = c.id "
        "FROM categories c WHERE lc.category = c.name"
    ))
    op.alter_column('laptop_customizations', 'category_id', nullable=False)
    op.create_foreign_key(
        'fk_laptop_customizations_category_id_categories',
        'laptop_customizations', 'categories', ['category_id'], ['id'],
    )
    op.drop_column('laptop_customizations', 'category')

    # -- 4. laptop_user_preference.budget: int -> {min, max} JSONB, casting
    #    existing values into the new shape (max = old ceiling, min = null).
    op.alter_column(
        'laptop_user_preference',
        'budget',
        type_=postgresql.JSONB(),
        postgresql_using="jsonb_build_object('min', NULL, 'max', budget)",
    )

    # -- 5. Seed product_types + questionnaire_questions -------------------
    product_types_table = sa.table(
        'product_types',
        sa.column('id', sa.Uuid()),
        sa.column('name', sa.String()),
        sa.column('is_active', sa.Boolean()),
        sa.column('created_at', sa.DateTime()),
    )
    laptop_product_type_id = uuid.uuid4()
    op.bulk_insert(product_types_table, [
        {"id": laptop_product_type_id, "name": "laptop", "is_active": True, "created_at": now},
    ])

    questionnaire_table = sa.table(
        'questionnaire_questions',
        sa.column('id', sa.Uuid()),
        sa.column('product_type_id', sa.Uuid()),
        sa.column('step_order', sa.Integer()),
        sa.column('question_text', sa.String()),
        sa.column('question_type', sa.String()),
        sa.column('target_field', sa.String()),
        sa.column('options', postgresql.JSON()),
        sa.column('help_text', sa.String()),
        sa.column('is_active', sa.Boolean()),
        sa.column('created_at', sa.DateTime()),
    )
    op.bulk_insert(questionnaire_table, [
        {
            "id": uuid.uuid4(),
            "product_type_id": laptop_product_type_id,
            "step_order": q["step_order"],
            "question_text": q["question_text"],
            "question_type": q["question_type"],
            "target_field": q["target_field"],
            "options": q["options"],
            "help_text": q["help_text"],
            "is_active": True,
            "created_at": now,
        }
        for q in _QUESTIONNAIRE_SEED
    ])


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'laptop_user_preference',
        'budget',
        type_=sa.INTEGER(),
        postgresql_using="(budget->>'max')::integer",
    )

    op.add_column('laptop_customizations', sa.Column('category', sa.VARCHAR(), nullable=True))
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE laptop_customizations lc SET category = c.name "
        "FROM categories c WHERE lc.category_id = c.id"
    ))
    op.alter_column('laptop_customizations', 'category', nullable=False)
    op.drop_constraint(
        'fk_laptop_customizations_category_id_categories',
        'laptop_customizations', type_='foreignkey',
    )
    op.drop_column('laptop_customizations', 'category_id')

    op.drop_table('laptop_categories')
    op.drop_index(op.f('ix_questionnaire_questions_step_order'), table_name='questionnaire_questions')
    op.drop_index(op.f('ix_questionnaire_questions_product_type_id'), table_name='questionnaire_questions')
    op.drop_table('questionnaire_questions')
    sa.Enum(name='questiontype').drop(op.get_bind(), checkfirst=True)
    op.drop_index(op.f('ix_product_types_name'), table_name='product_types')
    op.drop_table('product_types')
    op.drop_index(op.f('ix_categories_name'), table_name='categories')
    op.drop_table('categories')
