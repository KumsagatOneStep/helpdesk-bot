from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from helpdesk import clock


def _now() -> datetime:
    return clock.utcnow()


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    wa_chat_id: Mapped[str] = mapped_column(String(64), unique=True)
    phone: Mapped[str] = mapped_column(String(32))
    wa_name: Mapped[str | None] = mapped_column(String(200))
    full_name: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    intake_step: Mapped[str] = mapped_column(String(16), default="lang")
    intake_rejects: Mapped[int] = mapped_column(default=0, server_default="0")
    intake_muted_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(64))
    is_admin: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    on_shift: Mapped[bool] = mapped_column(default=True)
    active_ticket_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        Index(
            "uq_tickets_open_per_client",
            "client_id",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    status: Mapped[str] = mapped_column(String(16), default="new")
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    first_taken_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    last_broadcast_at: Mapped[datetime | None]
    broadcast_seq: Mapped[int] = mapped_column(default=0)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), index=True)
    direction: Mapped[str] = mapped_column(String(3))  # in | out
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    kind: Mapped[str] = mapped_column(String(16))  # text|image|document|audio|video|other
    text: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(String(500))
    file_name: Mapped[str | None] = mapped_column(String(255))
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    intake: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TicketEvent(Base):
    __tablename__ = "ticket_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # created|taken|returned|closed|delivery_failed
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class TgCard(Base):
    __tablename__ = "tg_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"))
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int] = mapped_column(BigInteger)
    has_button: Mapped[bool] = mapped_column(default=False)
    state: Mapped[str] = mapped_column(String(16), default="open")
    sent_at: Mapped[datetime] = mapped_column(default=_now)


class TgLink(Base):
    __tablename__ = "tg_links"
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_pending", "status", "next_attempt_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(2))  # wa | tg
    chat_key: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(default=_now)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=_now)
