"""intake reprompt limit

Revision ID: b3c9d1e2f4a5
Revises: 7a314f739f21
Create Date: 2026-10-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c9d1e2f4a5"
down_revision: str | Sequence[str] | None = "7a314f739f21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("intake_rejects", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "clients", sa.Column("intake_muted_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("clients", "intake_muted_until")
    op.drop_column("clients", "intake_rejects")
