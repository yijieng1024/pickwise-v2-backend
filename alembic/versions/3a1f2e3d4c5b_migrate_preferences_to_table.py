"""Migrate preferences from users to laptop_user_preference table

Revision ID: 3a1f2e3d4c5b
Revises: 2c9080e9c975
Create Date: 2026-05-31 16:15:00.000000

"""
from typing import Sequence, Union
from uuid import uuid4
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '3a1f2e3d4c5b'
down_revision: Union[str, Sequence[str], None] = '2c9080e9c975'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    
    # Create the new laptop_user_preference table
    op.create_table(
        'laptop_user_preference',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('budget', sa.Integer(), nullable=True),
        sa.Column('purpose', postgresql.JSON(), nullable=True),
        sa.Column('priorities', postgresql.JSON(), nullable=True),
        sa.Column('screen_size', postgresql.JSON(), nullable=True),
        sa.Column('portability', sa.String(), nullable=True),
        sa.Column('brand_preferences', postgresql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_laptop_user_preference_user_id'), 'laptop_user_preference', ['user_id'], unique=False)
    
    # Migrate data from preferences JSON column to new table
    connection = op.get_bind()
    users_table = sa.table('users',
        sa.column('id', postgresql.UUID(as_uuid=True)),
        sa.column('preferences', postgresql.JSON())
    )
    
    laptop_prefs_table = sa.table('laptop_user_preference',
        sa.column('id', postgresql.UUID(as_uuid=True)),
        sa.column('user_id', postgresql.UUID(as_uuid=True)),
        sa.column('budget', sa.Integer()),
        sa.column('purpose', postgresql.JSON()),
        sa.column('priorities', postgresql.JSON()),
        sa.column('screen_size', postgresql.JSON()),
        sa.column('portability', sa.String()),
        sa.column('brand_preferences', postgresql.JSON()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime())
    )
    
    # Get all users with preferences
    result = connection.execute(sa.select(users_table).where(users_table.c.preferences != None))
    rows = result.fetchall()
    
    now = sa.func.now()
    
    for row in rows:
        user_id = row[0]
        preferences = row[1]
        
        if preferences:
            # Extract fields from JSON preferences
            budget = preferences.get('budget')
            purpose = preferences.get('purpose', [])
            priorities = preferences.get('priorities', {})
            screen_size = preferences.get('screen_size', [])
            portability = preferences.get('portability')
            brand_preferences = preferences.get('brand_preferences', [])
            
            # Insert into new table
            insert_stmt = laptop_prefs_table.insert().values(
                id=uuid4(),
                user_id=user_id,
                budget=budget,
                purpose=purpose if purpose else None,
                priorities=priorities if priorities else None,
                screen_size=screen_size if screen_size else None,
                portability=portability,
                brand_preferences=brand_preferences if brand_preferences else None,
                created_at=now,
                updated_at=now
            )
            connection.execute(insert_stmt)
    
    # Drop the preferences column from users table
    op.drop_column('users', 'preferences')


def downgrade() -> None:
    """Downgrade schema."""
    
    # Add preferences column back to users
    op.add_column('users', sa.Column('preferences', postgresql.JSON(), nullable=True))
    
    # Migrate data back from laptop_user_preference to users.preferences
    connection = op.get_bind()
    laptop_prefs_table = sa.table('laptop_user_preference',
        sa.column('user_id', postgresql.UUID(as_uuid=True)),
        sa.column('budget', sa.Integer()),
        sa.column('purpose', postgresql.JSON()),
        sa.column('priorities', postgresql.JSON()),
        sa.column('screen_size', postgresql.JSON()),
        sa.column('portability', sa.String()),
        sa.column('brand_preferences', postgresql.JSON())
    )
    
    users_table = sa.table('users',
        sa.column('id', postgresql.UUID(as_uuid=True)),
        sa.column('preferences', postgresql.JSON())
    )
    
    result = connection.execute(sa.select(laptop_prefs_table))
    rows = result.fetchall()
    
    for row in rows:
        user_id = row[0]
        
        # Reconstruct preferences JSON
        preferences = {
            'budget': row[1],
            'purpose': row[2] or [],
            'priorities': row[3] or {},
            'screen_size': row[4] or [],
            'portability': row[5],
            'brand_preferences': row[6] or []
        }
        
        update_stmt = users_table.update().where(users_table.c.id == user_id).values(preferences=preferences)
        connection.execute(update_stmt)
    
    # Drop the laptop_user_preference table
    op.drop_table('laptop_user_preference')
