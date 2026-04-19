"""add jobs table and indexes

Revision ID: 4d2c80b21aa5
Revises: f20ffbfb5e81
Create Date: 2026-04-19 01:38:34.040662

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4d2c80b21aa5'
down_revision: Union[str, Sequence[str], None] = 'f20ffbfb5e81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('jobs',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('team_member_id', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='pending'),
        sa.Column('result_json', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_jobs_user_id', 'jobs', ['user_id'])
    op.create_index('ix_jobs_status', 'jobs', ['status'])
    op.create_index('ix_jobs_created_at', 'jobs', ['created_at'])

    op.create_index('ix_estimates_user_created', 'estimates', ['user_id', 'created_at'])
    op.create_index('ix_estimates_capture_mode', 'estimates', ['capture_mode'])
    op.create_index('ix_estimates_scene_type', 'estimates', ['scene_type'])
    op.create_index('ix_estimates_review_status', 'estimates', ['review_status'])


def downgrade() -> None:
    op.drop_index('ix_estimates_review_status', table_name='estimates')
    op.drop_index('ix_estimates_scene_type', table_name='estimates')
    op.drop_index('ix_estimates_capture_mode', table_name='estimates')
    op.drop_index('ix_estimates_user_created', table_name='estimates')
    op.drop_index('ix_jobs_created_at', table_name='jobs')
    op.drop_index('ix_jobs_status', table_name='jobs')
    op.drop_index('ix_jobs_user_id', table_name='jobs')
    op.drop_table('jobs')