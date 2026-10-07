from helpdesk import texts
from helpdesk.db.models import Client
from helpdesk.delivery import outbox
from helpdesk.whatsapp.parse import IncomingMessage

_LANGS = {
    "1": "ru",
    "ru": "ru",
    "рус": "ru",
    "русский": "ru",
    "2": "kk",
    "kk": "kk",
    "kz": "kk",
    "kaz": "kk",
    "каз": "kk",
    "қаз": "kk",
    "қазақша": "kk",
    "казахский": "kk",
}


def parse_lang(text: str | None) -> str | None:
    if not text:
        return None
    return _LANGS.get(text.strip().lower().rstrip("."))


def start(s, client: Client) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.CHOOSE_LANG)


def _say(s, client: Client, key: str) -> None:
    outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, key))


def advance(s, client: Client, msg: IncomingMessage) -> bool:
    text = (msg.text or "").strip() if msg.kind == "text" else ""
    if client.intake_step == "lang":
        lang = parse_lang(text)
        if lang is None:
            start(s, client)
            return False
        client.lang = lang
        client.intake_step = "name"
        _say(s, client, "ask_name")
        return False
    if client.intake_step == "name":
        if not 2 <= len(text) <= 100:
            _say(s, client, "bad_name")
            return False
        client.full_name = text
        client.intake_step = "location"
        _say(s, client, "ask_location")
        return False
    if client.intake_step == "location":
        if not 1 <= len(text) <= 200:
            _say(s, client, "bad_location")
            return False
        client.location = text
        client.intake_step = "done"
        return True
    return True
