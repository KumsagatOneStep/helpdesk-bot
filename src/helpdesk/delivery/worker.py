import asyncio
from datetime import timedelta
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import aliased

from helpdesk import clock
from helpdesk.db import queries
from helpdesk.db.models import Outbox, Staff, TgCard, TgLink, Ticket, TicketEvent
from helpdesk.delivery import outbox

log = structlog.get_logger()

RETRY_DELAYS = [5, 30, 120, 600, 1800]


class PermanentSendError(Exception):
    """Повтор не поможет (бот заблокирован, файл отвергнут) — сразу failed."""


class WaSender(Protocol):
    async def send_text(self, chat_id: str, text: str) -> str: ...

    async def send_file(
        self, chat_id: str, path: str, file_name: str, caption: str | None
    ) -> str: ...


class TgSender(Protocol):
    async def send(
        self,
        chat_id: int,
        *,
        text: str | None,
        file_path: str | None,
        file_kind: str | None,
        buttons: list | None,
    ) -> int: ...

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: list | None
    ) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...


async def process_batch(sf, wa: WaSender, tg: TgSender, limit: int = 20) -> int:
    prev = aliased(Outbox)
    blocked = (
        select(prev.id)
        .where(prev.chat_key == Outbox.chat_key, prev.status == "pending", prev.id < Outbox.id)
        .exists()
    )
    async with sf() as s:
        q = (
            select(Outbox.id)
            .where(Outbox.status == "pending", Outbox.next_attempt_at <= clock.utcnow(), ~blocked)
            .order_by(Outbox.id)
            .limit(limit)
        )
        ids = list((await s.scalars(q)).all())
    for item_id in ids:
        await _process_one(sf, wa, tg, item_id)
    return len(ids)


async def run(sf, wa: WaSender, tg: TgSender, stop: asyncio.Event, idle_sleep: float = 1.0):
    while not stop.is_set():
        try:
            n = await process_batch(sf, wa, tg)
        except Exception:
            log.exception("outbox batch failed")
            n = 0
        if n == 0:
            try:
                await asyncio.wait_for(stop.wait(), idle_sleep)
            except TimeoutError:
                pass


async def _process_one(sf, wa, tg, item_id: int) -> None:
    async with sf() as s, s.begin():
        item = await s.get(Outbox, item_id, with_for_update=True)
        if item is None or item.status != "pending":
            return
        try:
            async with s.begin_nested():
                await _deliver(s, item, wa, tg)
        except Exception as e:
            log.warning("outbox delivery failed", id=item_id, error=str(e))
            await s.refresh(item)  # откат savepoint мог сделать объект устаревшим
            item.attempts += 1
            item.last_error = str(e)[:1000]
            if isinstance(e, PermanentSendError) or item.attempts > len(RETRY_DELAYS):
                item.status = "failed"
                await _on_failed(s, item)
            else:
                delay = RETRY_DELAYS[item.attempts - 1]
                item.next_attempt_at = clock.utcnow() + timedelta(seconds=delay)
            return
        if item.status == "pending":
            item.status = "sent"


async def _deliver(s, item: Outbox, wa, tg) -> None:
    p = item.payload
    op = p["op"]
    if item.channel == "wa":
        if op == "text":
            await wa.send_text(p["chat_id"], p["text"])
        else:
            await wa.send_file(p["chat_id"], p["path"], p["file_name"], p.get("caption"))
        return
    if op == "send":
        if p.get("card_seq") is not None:
            # FOR SHARE до commit: take/рассылка ждут, пока карточка не запишется,
            # и затем видят её (иначе — карточка-сирота с живой кнопкой).
            t = await s.get(
                Ticket, p["ticket_id"], with_for_update={"read": True}, populate_existing=True
            )
            if t is None or t.status != "new" or t.broadcast_seq != p["card_seq"]:
                item.status = "skipped"
                return
        mid = await tg.send(
            p["chat_id"],
            text=p.get("text"),
            file_path=p.get("file_path"),
            file_kind=p.get("file_kind"),
            buttons=p.get("buttons"),
        )
        if p.get("ticket_id") is not None:
            await s.merge(
                TgLink(tg_chat_id=p["chat_id"], tg_message_id=mid, ticket_id=p["ticket_id"])
            )
        if p.get("card_staff_id") is not None:
            s.add(
                TgCard(
                    ticket_id=p["ticket_id"],
                    staff_id=p["card_staff_id"],
                    tg_chat_id=p["chat_id"],
                    tg_message_id=mid,
                    has_button=bool(p.get("buttons")),
                )
            )
    elif op == "edit":
        await tg.edit(p["chat_id"], p["message_id"], p["text"], p.get("buttons"))
    elif op == "delete_card":
        card = await s.get(TgCard, p["card_id"])
        if card is None or card.state != "deleting":
            item.status = "skipped"
            return
        try:
            await tg.delete(card.tg_chat_id, card.tg_message_id)
            card.state = "deleted"
        except Exception:
            try:
                await tg.edit(card.tg_chat_id, card.tg_message_id, p["fallback_text"], None)
            except Exception as e:  # карточка-файл без текста — оставляем как есть
                log.info("card edit fallback failed", card_id=card.id, error=str(e))
            card.state = "marked"


async def _on_failed(s, item: Outbox) -> None:
    p = item.payload
    if item.channel != "wa" or p.get("ticket_id") is None:
        return
    t = await s.get(Ticket, p["ticket_id"])
    if t is None:
        return
    s.add(TicketEvent(ticket_id=t.id, type="delivery_failed"))
    preview = (p.get("text") or p.get("file_name") or "")[:200]
    text = f"⚠️ Не доставлено в #{t.id}: {preview}"
    assignee = await s.get(Staff, t.assignee_id) if t.assignee_id else None
    recipients = [assignee] if assignee and assignee.status == "active" else []
    recipients = recipients or await queries.active_admins(s)
    for st in recipients:
        outbox.tg_send(s, st.tg_chat_id, text=text, ticket_id=t.id)
