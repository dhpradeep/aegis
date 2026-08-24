"""nullable api_key_id on usage/completion_logs/objectives (key deletion)

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b2d4f6a8c0e1'
down_revision: Union[str, None] = 'a1c3e5f7b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('usage', 'completion_logs', 'objectives')


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column('api_key_id', existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.alter_column('api_key_id', existing_type=sa.String(), nullable=False)
