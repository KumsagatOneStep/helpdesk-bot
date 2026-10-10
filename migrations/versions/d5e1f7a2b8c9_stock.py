"""stock

Revision ID: d5e1f7a2b8c9
Revises: c4d2e8f1a6b7
Create Date: 2026-10-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e1f7a2b8c9"
down_revision: str | Sequence[str] | None = "c4d2e8f1a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("min_qty", sa.Integer(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index(
        "uq_stock_items_active_name",
        "stock_items",
        ["category", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("NOT archived"),
    )
    op.create_table(
        "stock_levels",
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("stock_items.id"), primary_key=True),
        sa.Column("condition", sa.String(16), primary_key=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("qty >= 0", name="ck_stock_levels_qty"),
    )
    op.create_table(
        "stock_moves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("stock_items.id"), nullable=False),
        sa.Column("condition", sa.String(16), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_stock_moves_item_id", "stock_moves", ["item_id"])
    op.create_index("ix_stock_moves_created_at", "stock_moves", ["created_at"])


def downgrade() -> None:
    op.drop_table("stock_moves")
    op.drop_table("stock_levels")
    op.drop_index("uq_stock_items_active_name", table_name="stock_items")
    op.drop_table("stock_items")
