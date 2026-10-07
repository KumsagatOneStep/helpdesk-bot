import re
from typing import NamedTuple

from helpdesk import texts
from helpdesk.db.models import Client
from helpdesk.delivery import outbox
from helpdesk.whatsapp.parse import IncomingMessage

_DIGITS = re.compile(r"([12])(?!\d)")
_PREFIXES = {"ru": "ru", "рус": "ru", "ka": "kk", "kz": "kk", "kk": "kk", "каз": "kk", "қаз": "kk"}


def parse_lang(text: str | None) -> str | None:
    t = (text or "").strip().lower()
    if m := _DIGITS.match(t):
        return "ru" if m.group(1) == "1" else "kk"
    for prefix, lang in _PREFIXES.items():
        if t.startswith(prefix):
            return lang
    return None


def start(s, client: Client) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.CHOOSE_LANG)


def _say(s, client: Client, key: str) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, key))


class Step(NamedTuple):
    consumed: bool  # сообщение принято как ответ на вопрос анкеты
    done: bool  # анкета завершена


_REJECTED = Step(consumed=False, done=False)


def advance(s, client: Client, msg: IncomingMessage) -> Step:
    text = (msg.text or "").strip() if msg.kind == "text" else ""
    if client.intake_step == "lang":
        lang = parse_lang(text)
        if lang is None:
            start(s, client)
            return _REJECTED
        client.lang = lang
        client.intake_step = "name"
        _say(s, client, "ask_name")
        return Step(consumed=True, done=False)
    if client.intake_step == "name":
        if not 2 <= len(text) <= 100:
            _say(s, client, "bad_name")
            return _REJECTED
        client.full_name = text
        client.intake_step = "location"
        _say(s, client, "ask_location")
        return Step(consumed=True, done=False)
    if client.intake_step == "location":
        if not 1 <= len(text) <= 200:
            _say(s, client, "bad_location")
            return _REJECTED
        client.location = text
        client.intake_step = "done"
        return Step(consumed=True, done=True)
    return Step(consumed=False, done=True)
