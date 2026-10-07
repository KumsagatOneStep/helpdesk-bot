import asyncio

from sqlalchemy import select

from helpdesk.db.models import Outbox, Staff, TgCard, TgLink, TicketEvent
from helpdesk.delivery import outbox, worker
from helpdesk.tickets import service as tickets
from tests.factories import make_client, make_staff, make_ticket
from tests.fakes import FakeTg, FakeWa


async def drain(sf, wa, tg) -> int:
    total = 0
    while n := await worker.process_batch(sf, wa, tg):
        total += n
    return total


async def test_sends_in_order_per_chat(sf, clock_):
    async with sf() as s, s.begin():
        outbox.wa_text(s, "1@c.us", "a")
        outbox.wa_text(s, "1@c.us", "b")
        outbox.wa_text(s, "2@c.us", "x")
        outbox.wa_text(s, "1@c.us", "c")
    wa, tg = FakeWa(), FakeTg()
    await drain(sf, wa, tg)
    assert [m[2] for m in wa.sent if m[1] == "1@c.us"] == ["a", "b", "c"]
    assert len(wa.sent) == 4


async def test_failed_head_blocks_rest_of_chat(sf, clock_):
    async with sf() as s, s.begin():
        outbox.wa_text(s, "1@c.us", "a")
        outbox.wa_text(s, "1@c.us", "b")
        outbox.wa_text(s, "2@c.us", "x")
    wa, tg = FakeWa(fail_chats={"1@c.us"}), FakeTg()
    await drain(sf, wa, tg)
    assert wa.sent == [("text", "2@c.us", "x")]
    wa.fail_chats.clear()
    await drain(sf, wa, tg)  # пауза ещё не прошла
    assert len(wa.sent) == 1
    clock_.advance(seconds=5)
    await drain(sf, wa, tg)
    assert [m[2] for m in wa.sent] == ["x", "a", "b"]


async def test_gives_up_after_retries_and_warns_assignee(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c, status="in_progress", assignee=erlan)
        outbox.wa_text(s, c.wa_chat_id, "Сейчас подойду", ticket_id=t.id)
        tid = t.id
    wa, tg = FakeWa(fail_chats={"77011234567@c.us"}), FakeTg()
    for delay in [0, *worker.RETRY_DELAYS]:
        clock_.advance(seconds=delay)
        await drain(sf, wa, tg)
    async with sf() as s:
        item = await s.scalar(select(Outbox).where(Outbox.channel == "wa"))
        assert item.status == "failed"
        assert item.attempts == len(worker.RETRY_DELAYS) + 1
        types = (
            await s.scalars(select(TicketEvent.type).where(TicketEvent.ticket_id == tid))
        ).all()
        assert "delivery_failed" in types
    assert any(
        m["chat_id"] == 1 and m["text"].startswith(f"⚠️ Не доставлено в #{tid}") for m in tg.sent
    )


async def test_card_send_records_card_and_link(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(
            s,
            1,
            text="card",
            buttons=[[("✋ Взять", f"take:{t.id}")]],
            ticket_id=t.id,
            card_staff_id=st.id,
            card_seq=t.broadcast_seq,
        )
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent[0]["buttons"] == [[["✋ Взять", "take:1"]]]
    async with sf() as s:
        card = await s.scalar(select(TgCard))
        assert card.has_button and card.state == "open"
        assert card.tg_message_id == tg.sent[0]["mid"]
        link = await s.get(TgLink, (1, card.tg_message_id))
        assert link.ticket_id == 1


async def test_stale_card_is_skipped(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(s, 1, text="old", ticket_id=t.id, card_staff_id=st.id, card_seq=0)
        t.broadcast_seq = 1
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent == []
    async with sf() as s:
        assert (await s.scalar(select(Outbox))).status == "skipped"


async def test_card_for_taken_ticket_is_skipped(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        outbox.tg_send(s, 1, text="card", ticket_id=t.id, card_staff_id=st.id, card_seq=0)
        t.status = "in_progress"
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.sent == []


async def test_delete_card_falls_back_to_edit(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        st = await make_staff(s, tg_id=1, name="Ерлан")
        t = await make_ticket(s, c)
        ok = TgCard(ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=54, has_button=True)
        old = TgCard(
            ticket_id=t.id, staff_id=st.id, tg_chat_id=1, tg_message_id=55, has_button=True
        )
        s.add_all([ok, old])
        await s.flush()
        outbox.tg_delete_card(s, ok, "🔒 #1 взял Тимур")
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.deleted == [(1, 54)]
    async with sf() as s, s.begin():
        old = await s.scalar(select(TgCard).where(TgCard.tg_message_id == 55))
        outbox.tg_delete_card(s, old, "🔒 #1 взял Тимур")
    tg.fail_delete = True
    await drain(sf, FakeWa(), tg)
    assert tg.edited == [(1, 55, "🔒 #1 взял Тимур", None)]
    async with sf() as s:
        states = dict((await s.execute(select(TgCard.tg_message_id, TgCard.state))).all())
        assert states == {54: "deleted", 55: "marked"}


async def test_tg_edit_is_sent(sf, clock_):
    async with sf() as s, s.begin():
        outbox.tg_edit(s, 1, 77, "новый текст", [[("✅ Закрыть", "close:1")]])
    tg = FakeTg()
    await drain(sf, FakeWa(), tg)
    assert tg.edited == [(1, 77, "новый текст", [[["✅ Закрыть", "close:1"]]])]


class TakeDuringSendTg(FakeTg):
    """Пока карточка «летит» в Telegram, другой специалист берёт заявку."""

    def __init__(self, sf, ticket_id, taker_id):
        super().__init__()
        self.sf, self.ticket_id, self.taker_id = sf, ticket_id, taker_id
        self.task = None

    async def _take(self):
        async with self.sf() as s, s.begin():
            taker = await s.get(Staff, self.taker_id)
            return await tickets.take(s, self.ticket_id, taker)

    async def send(self, chat_id, **kw):
        if self.task is None:
            self.task = asyncio.create_task(self._take())
            await asyncio.wait({self.task}, timeout=0.3)  # даём take шанс завершиться
        return await super().send(chat_id, **kw)


async def test_take_during_card_send_sees_the_new_card(sf, clock_):
    async with sf() as s, s.begin():
        c = await make_client(s)
        erlan = await make_staff(s, tg_id=1, name="Ерлан")
        timur = await make_staff(s, tg_id=2, name="Тимур")
        t = await make_ticket(s, c)
        outbox.tg_send(
            s,
            1,
            text="card",
            buttons=[[("✋ Взять", f"take:{t.id}")]],
            ticket_id=t.id,
            card_staff_id=erlan.id,
            card_seq=t.broadcast_seq,
        )
        tid, timur_id = t.id, timur.id
    tg = TakeDuringSendTg(sf, tid, timur_id)
    await drain(sf, FakeWa(), tg)
    assert (await tg.task).ok
    async with sf() as s:
        card = await s.scalar(select(TgCard).where(TgCard.staff_id != timur_id))
        assert card.state == "deleting"
