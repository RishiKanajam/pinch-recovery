"""add pinch live ids

Revision ID: a1b2c3d4e5f6
Revises: 5f677b54688c
Create Date: 2026-07-26 06:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5f677b54688c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('pinch_payer_id', sa.String(length=40), nullable=True))
    op.add_column('customers', sa.Column('pinch_source_id', sa.String(length=40), nullable=True))
    op.add_column('payments', sa.Column('pinch_payment_id', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('payments', 'pinch_payment_id')
    op.drop_column('customers', 'pinch_source_id')
    op.drop_column('customers', 'pinch_payer_id')
