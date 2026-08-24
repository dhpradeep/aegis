"""sessions: conv_key (explicit client conversation id)

Revision ID: c3e5a7b9d1f3
Revises: b2d4f6a8c0e1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3e5a7b9d1f3'
down_revision: Union[str, None] = 'b2d4f6a8c0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.add_column(sa.Column('conv_key', sa.String(), nullable=True))
        batch.create_index('ix_sessions_conv_key', ['conv_key'])


def downgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.drop_index('ix_sessions_conv_key')
        batch.drop_column('conv_key')
