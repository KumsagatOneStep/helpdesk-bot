from datetime import UTC, datetime

from helpdesk.db.models import Client, Message, Ticket
from helpdesk.tickets import render

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)  # 14:00 по Алматы


def _client():
    return Client(
        phone="77011234567", full_name="Айгерим Сериккызы", location="Бухгалтерия, каб. 214"
    )


def test_queue_card_layout():
    t = Ticket(id=125, created_at=T0)
    msgs = [
        Message(direction="in", kind="text", text="Не печатает", created_at=T0),
        Message(direction="in", kind="image", text=None, file_path="/x.jpg", created_at=T0),
    ]
    text = render.queue_card(t, _client(), msgs)
    assert text.splitlines()[0] == "🆕 Заявка #125 · 14:00"
    assert "👤 Айгерим Сериккызы · Бухгалтерия, каб. 214" in text
    assert "📞 +77011234567" in text
    assert "«Не печатает»" in text and "«[🖼 фото]»" in text


def test_queue_card_reminder_header():
    t = Ticket(id=7, created_at=T0)
    assert render.queue_card(t, _client(), [], waited_min=20).startswith("⏰ Заявка #7 ждёт 20 мин")


def test_queue_card_clips_long_text():
    t = Ticket(id=1, created_at=T0)
    msgs = [Message(direction="in", kind="text", text="x" * 5000 + "КОНЕЦ", created_at=T0)]
    text = render.queue_card(t, _client(), msgs)
    assert len(text) <= 4000
    assert text.startswith("🆕 Заявка #1") and "КОНЕЦ" in text


def test_history_marks_directions_and_names():
    msgs = [
        Message(direction="in", kind="text", text="Помогите", created_at=T0),
        Message(direction="out", staff_id=3, kind="text", text="Иду", created_at=T0),
    ]
    h = render.history(msgs, {3: "Ерлан"})
    assert h.splitlines() == ["07.10 14:00 👤: Помогите", "07.10 14:00 🛠 Ерлан: Иду"]


def test_client_line_falls_back_to_whatsapp_name():
    c = Client(phone="7701", wa_name="Aigerim", full_name=None, location=None)
    assert render.client_line(c) == "👤 Aigerim · —\n📞 +7701"
