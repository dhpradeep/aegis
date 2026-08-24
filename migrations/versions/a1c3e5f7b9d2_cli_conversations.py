"""sessions: conv_hash/conv_turns for CLI conversation routing

Revision ID: a1c3e5f7b9d2
Revises: 78fd73ddcbad
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1c3e5f7b9d2'
down_revision: Union[str, None] = '78fd73ddcbad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.add_column(sa.Column('conv_hash', sa.String(), nullable=True))
        batch.add_column(sa.Column('conv_turns', sa.Integer(), nullable=False, server_default='0'))
        batch.create_index('ix_sessions_conv_hash', ['conv_hash'])


def downgrade() -> None:
    with op.batch_alter_table('sessions') as batch:
        batch.drop_index('ix_sessions_conv_hash')
        batch.drop_column('conv_turns')
        batch.drop_column('conv_hash')
