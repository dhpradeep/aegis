"""initial schema

Revision ID: 78fd73ddcbad
Revises: 
Create Date: 2026-08-12 17:02:46.573345
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '78fd73ddcbad'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('model', sa.String(), nullable=False),
    sa.Column('effort', sa.String(), nullable=True),
    sa.Column('system_prompt', sa.String(), nullable=True),
    sa.Column('allowed_tools_json', sa.String(), nullable=False),
    sa.Column('permission_mode', sa.String(), nullable=False),
    sa.Column('mcp_names_json', sa.String(), nullable=False),
    sa.Column('roster_json', sa.String(), nullable=False),
    sa.Column('max_cost_usd', sa.Float(), nullable=True),
    sa.Column('max_iterations', sa.Integer(), nullable=False),
    sa.Column('is_admin_only', sa.Boolean(), nullable=False),
    sa.Column('bypass_permissions', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.Column('detail_json', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tenants',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('default_model', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('key_hash', sa.String(), nullable=False),
    sa.Column('prefix', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('rpm', sa.Integer(), nullable=True),
    sa.Column('daily_cost_usd', sa.Float(), nullable=True),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_table('billing_configs',
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('price_per_mtok_input', sa.Float(), nullable=False),
    sa.Column('price_per_mtok_output', sa.Float(), nullable=False),
    sa.Column('markup', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id')
    )
    op.create_table('mcp_servers',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('url', sa.String(), nullable=True),
    sa.Column('headers_json', sa.String(), nullable=True),
    sa.Column('command', sa.String(), nullable=True),
    sa.Column('args_json', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'name')
    )
    op.create_table('sessions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('profile', sa.String(), nullable=False),
    sa.Column('agent_id', sa.String(), nullable=True),
    sa.Column('overrides_json', sa.String(), nullable=False),
    sa.Column('mcp_names_json', sa.String(), nullable=False),
    sa.Column('workspace_path', sa.String(), nullable=False),
    sa.Column('sdk_session_id', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('title', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('webhook_configs',
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('url', sa.String(), nullable=False),
    sa.Column('secret', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id')
    )
    op.create_table('completion_logs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('api_key_id', sa.String(), nullable=False),
    sa.Column('model', sa.String(), nullable=False),
    sa.Column('streamed', sa.Boolean(), nullable=False),
    sa.Column('request_json', sa.String(), nullable=False),
    sa.Column('response_text', sa.String(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('session_id', sa.String(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('payload_json', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('jobs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('prompt', sa.String(), nullable=False),
    sa.Column('result_json', sa.String(), nullable=True),
    sa.Column('error', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('objectives',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('api_key_id', sa.String(), nullable=False),
    sa.Column('agent_id', sa.String(), nullable=False),
    sa.Column('goal', sa.String(), nullable=False),
    sa.Column('rubric', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('max_cost_usd', sa.Float(), nullable=True),
    sa.Column('max_iterations', sa.Integer(), nullable=False),
    sa.Column('iterations_done', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('result_text', sa.String(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('rate_buckets',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('api_key_id', sa.String(), nullable=False),
    sa.Column('window', sa.String(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('api_key_id', 'window', name='uq_rate_buckets_api_key_id_window')
    )
    op.create_table('usage',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('api_key_id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('num_turns', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('usage')
    op.drop_table('rate_buckets')
    op.drop_table('objectives')
    op.drop_table('jobs')
    op.drop_table('events')
    op.drop_table('completion_logs')
    op.drop_table('webhook_configs')
    op.drop_table('sessions')
    op.drop_table('mcp_servers')
    op.drop_table('billing_configs')
    op.drop_table('api_keys')
    op.drop_table('tenants')
    op.drop_table('audit_logs')
    op.drop_table('agents')
