"""sessions: origin (api | cli | portal)

Revision ID: e5a7c9d1f3b5
Revises: d4f6b8c0e2a4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e5a7c9d1f3b5'
down_revision: Union[str, None] = 'd4f6b8c0e2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.add_column(sa.Column('origin', sa.String(), nullable=False, server_default='api'))
        batch.create_index('ix_sessions_origin', ['origin'])
    op.execute("UPDATE sessions SET origin='cli' WHERE conv_hash IS NOT NULL OR conv_key IS NOT NULL")


def downgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.drop_index('ix_sessions_origin')
        batch.drop_column('origin')
