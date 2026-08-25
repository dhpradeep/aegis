"""agents: portal_visible flag for the tenant chat portal

Revision ID: d4f6b8c0e2a4
Revises: c3e5a7b9d1f3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd4f6b8c0e2a4'
down_revision: Union[str, None] = 'c3e5a7b9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.add_column(sa.Column('portal_visible', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('agents') as batch:
        batch.drop_column('portal_visible')
