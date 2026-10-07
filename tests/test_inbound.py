import pytest
from sqlalchemy import func, select

from helpdesk import texts
from helpdesk.db.models import Client, Message, Ticket
from helpdesk.inbound import handle_incoming
from helpdesk.intake.service import parse_lang
from helpdesk.whatsapp.parse import IncomingMessage
from tests.factories import make_client, make_staff, payloads

CHAT = "77011234567@c.us"


def incoming(text="Привет", *, mid="M1", kind="text", chat=CHAT, **kw) -> IncomingMessage:
    return IncomingMessage(
        wa_message_id=mid,
        chat_id=chat,
        phone=chat.split("@")[0],
        sender_name="Aigerim",
        kind=kind,
        text=text,
        **kw,
    )


async def _feed(s, *texts_):
    for i, t in enumerate(texts_):
        await handle_incoming(s, incoming(t, mid=f"M{i}"), None)


async def _wa_texts(s):
    return [p["text"] for p in await payloads(s, "wa")]


def test_parse_lang():
    assert parse_lang(" 1 ") == "ru" and parse_lang("Рус") == "ru"
    assert parse_lang("2") == "kk" and parse_lang("қазақша") == "kk" and parse_lang("KZ") == "kk"
    assert parse_lang("3") is None and parse_lang(None) is None


@pytest.mark.parametrize(
    "text,lang",
    [
        ("1)", "ru"),
        ("Rus", "ru"),
        ("русский язык", "ru"),
        ("1. Русский", "ru"),
        ("казакша", "kk"),
        ("Kazakh", "kk"),
        ("2)", "kk"),
        ("Қазақ тілі", "kk"),
        ("12", None),
        ("привет", None),
        ("!!!", None),
    ],
)
def test_parse_lang_prefixes(text, lang):
    assert parse_lang(text) == lang


async def test_full_intake_ru_creates_ticket_with_first_message(s, clock_):
    await make_staff(s, tg_id=1, name="Ерлан")
    await _feed(s, "Не печатает принтер", "1", "Айгерим Сериккызы", "Бухгалтерия, 214")
    c = await s.scalar(select(Client))
    assert (c.lang, c.full_name, c.location, c.intake_step, c.wa_name) == (
        "ru",
        "Айгерим Сериккызы",
        "Бухгалтерия, 214",
        "done",
        "Aigerim",
    )
    t = await s.scalar(select(Ticket))
    msgs = (await s.scalars(select(Message.text).where(Message.ticket_id == t.id))).all()
    assert msgs == ["Не печатает принтер"]
    assert await _wa_texts(s) == [
        texts.CHOOSE_LANG,
        texts.t("ru", "ask_name"),
        texts.t("ru", "ask_location"),
        "Заявка #1 принята. Специалист скоро ответит.",
    ]
    cards = [p for p in await payloads(s, "tg") if p.get("buttons")]
    assert len(cards) == 1 and "Бухгалтерия, 214" in cards[0]["text"]


async def test_full_intake_kk(s, clock_):
    await _feed(s, "Сәлем", "2", "Асқар Нұрланұлы", "Қойма, 5")
    assert await _wa_texts(s) == [
        texts.CHOOSE_LANG,
        texts.t("kk", "ask_name"),
        texts.t("kk", "ask_location"),
        "#1 өтінім қабылданды. Маман жақын арада жауап береді.",
    ]


async def test_unknown_language_answer_repeats_question(s, clock_):
    await _feed(s, "Привет", "3")
    assert await _wa_texts(s) == [texts.CHOOSE_LANG, texts.CHOOSE_LANG]
    assert (await s.scalar(select(Client))).intake_step == "lang"


async def test_name_must_be_text(s, clock_):
    await _feed(s, "Привет", "1")
    await handle_incoming(s, incoming(None, mid="P1", kind="image", download_url="u"), "/f/x.jpg")
    assert (await _wa_texts(s))[-1] == texts.t("ru", "bad_name")
    assert (await s.scalar(select(Client))).intake_step == "name"
    await handle_incoming(s, incoming("А", mid="P2"), None)  # 1 символ
    assert (await _wa_texts(s))[-1] == texts.t("ru", "bad_name")


async def test_duplicate_notification_ignored(s, clock_):
    await handle_incoming(s, incoming("Привет", mid="SAME"), None)
    await handle_incoming(s, incoming("Привет", mid="SAME"), None)
    assert await s.scalar(select(func.count()).select_from(Message)) == 1
    assert await _wa_texts(s) == [texts.CHOOSE_LANG]


async def test_known_client_opens_ticket_directly(s, clock_):
    await make_client(s)
    await handle_incoming(s, incoming("Не работает интернет", mid="K1"), None)
    t = await s.scalar(select(Ticket))
    assert t is not None
    assert await _wa_texts(s) == ["Заявка #1 принята. Специалист скоро ответит."]


async def test_file_path_is_stored(s, clock_):
    await make_client(s)
    msg = incoming("скрин", mid="F1", kind="image", download_url="u", file_name="a.jpg")
    await handle_incoming(s, msg, "/f/a.jpg")
    m = await s.scalar(select(Message))
    assert (m.kind, m.text, m.file_path, m.file_name) == ("image", "скрин", "/f/a.jpg", "a.jpg")


async def test_sticker_gets_unsupported_reply_and_goes_into_ticket(s, clock_):
    await make_client(s)
    await handle_incoming(s, incoming("[стикер]", mid="S1", kind="other"), None)
    assert await _wa_texts(s) == [
        texts.t("ru", "unsupported"),
        "Заявка #1 принята. Специалист скоро ответит.",
    ]
    m = await s.scalar(select(Message))
    assert m.ticket_id == 1 and m.text == "[стикер]"


async def test_messages_during_intake_join_ticket_but_answers_do_not(s, clock_):
    await _feed(s, "Не печатает принтер", "1")
    photo = incoming(None, mid="P1", kind="image", download_url="u", file_name="a.jpg")
    await handle_incoming(s, photo, "/f/a.jpg")  # вместо имени — фото
    await handle_incoming(s, incoming("А", mid="P2"), None)  # слишком короткое имя
    await handle_incoming(s, incoming("Айгерим", mid="P3"), None)
    await handle_incoming(s, incoming("Бухгалтерия", mid="P4"), None)
    t = await s.scalar(select(Ticket))
    q = select(Message.wa_message_id).where(Message.ticket_id == t.id).order_by(Message.id)
    assert (await s.scalars(q)).all() == ["M0", "P1", "P2"]
