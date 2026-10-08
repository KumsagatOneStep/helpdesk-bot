import re
from collections.abc import Callable
from datetime import timedelta
from typing import NamedTuple

from helpdesk import clock, texts
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

# автоответчик на номере не должен гонять анкету бесконечно: квота Green API и риск бана
MAX_REPROMPTS = 3
REPROMPT_PAUSE = timedelta(minutes=30)


def _reject(client: Client, reprompt: Callable[[], None]) -> Step:
    now = clock.utcnow()
    if client.intake_muted_until and now < client.intake_muted_until:
        return _REJECTED
    client.intake_rejects += 1
    if client.intake_rejects > MAX_REPROMPTS:
        client.intake_rejects = 0
        client.intake_muted_until = now + REPROMPT_PAUSE
        return _REJECTED
    reprompt()
    return _REJECTED


def _accepted(client: Client, done: bool) -> Step:
    client.intake_rejects = 0
    client.intake_muted_until = None
    return Step(consumed=True, done=done)


def advance(s, client: Client, msg: IncomingMessage) -> Step:
    text = (msg.text or "").strip() if msg.kind == "text" else ""
    if client.intake_step == "lang":
        lang = parse_lang(text)
        if lang is None:
            return _reject(client, lambda: start(s, client))
        client.lang = lang
        client.intake_step = "name"
        _say(s, client, "ask_name")
        return _accepted(client, done=False)
    if client.intake_step == "name":
        if not 2 <= len(text) <= 100:
            return _reject(client, lambda: _say(s, client, "bad_name"))
        client.full_name = text
        client.intake_step = "location"
        _say(s, client, "ask_location")
        return _accepted(client, done=False)
    if client.intake_step == "location":
        if not 1 <= len(text) <= 200:
            return _reject(client, lambda: _say(s, client, "bad_location"))
        client.location = text
        client.intake_step = "done"
        return _accepted(client, done=True)
    return Step(consumed=False, done=True)
