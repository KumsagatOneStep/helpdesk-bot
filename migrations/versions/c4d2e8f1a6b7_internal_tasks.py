"""internal tasks

Revision ID: c4d2e8f1a6b7
Revises: b3c9d1e2f4a5
Create Date: 2026-10-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d2e8f1a6b7"
down_revision: str | Sequence[str] | None = "b3c9d1e2f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "staff",
        sa.Column("role", sa.String(16), server_default="helpdesk", nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requester", sa.String(200), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("last_reminded_on", sa.Date(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_kind", sa.String(16), nullable=True),
        sa.Column("creator_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("closed_by_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("taken_at", TS, nullable=True),
        sa.Column("closed_at", TS, nullable=True),
    )
    op.create_index("ix_tasks_status_due", "tasks", ["status", "due_date"])
    op.create_table(
        "task_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_task_comments_task_id", "task_comments", ["task_id"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_table(
        "task_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("staff_id", sa.Integer(), sa.ForeignKey("staff.id"), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", TS, nullable=False),
    )
    op.create_index("ix_task_cards_task_id", "task_cards", ["task_id"])
    op.create_table(
        "task_links",
        sa.Column("tg_chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("tg_message_id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("task_links")
    op.drop_index("ix_task_cards_task_id", table_name="task_cards")
    op.drop_table("task_cards")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_task_comments_task_id", table_name="task_comments")
    op.drop_table("task_comments")
    op.drop_index("ix_tasks_status_due", table_name="tasks")
    op.drop_table("tasks")
    op.drop_column("staff", "role")
