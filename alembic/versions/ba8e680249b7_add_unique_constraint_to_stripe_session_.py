"""add unique constraint to stripe_session_id

Revision ID: ba8e680249b7
Revises: 4d2c80b21aa5
Create Date: 2026-04-25 02:27:27.098279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba8e680249b7'
down_revision: Union[str, Sequence[str], None] = '4d2c80b21aa5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unique constraint to credit_transactions.stripe_session_id."""
    # First, clear any empty strings that would conflict with unique constraint
    op.execute("UPDATE credit_transactions SET stripe_session_id = NULL WHERE stripe_session_id = ''")
    # Add unique constraint
    op.create_unique_constraint('uq_credit_transactions_stripe_session_id', 'credit_transactions', ['stripe_session_id'])


def downgrade() -> None:
    """Remove unique constraint from credit_transactions.stripe_session_id."""
    op.drop_constraint('uq_credit_transactions_stripe_session_id', 'credit_transactions', type_='unique')
