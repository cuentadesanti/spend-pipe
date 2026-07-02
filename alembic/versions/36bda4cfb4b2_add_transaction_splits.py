"""add transaction_splits

Revision ID: 36bda4cfb4b2
Revises: 384f2fb45f6c
Create Date: 2026-07-02 14:35:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "36bda4cfb4b2"
down_revision: Union[str, None] = "384f2fb45f6c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transaction_splits",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("txn_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["txn_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("transaction_splits", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_transaction_splits_txn_id"), ["txn_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("transaction_splits", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_splits_txn_id"))

    op.drop_table("transaction_splits")
