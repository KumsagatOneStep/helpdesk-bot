from dataclasses import replace
from datetime import timedelta

from sqlalchemy import func, select

from helpdesk import clock, texts
from helpdesk.db.models import Client, Message
from helpdesk.delivery import outbox
from helpdesk.intake import service as intake
from helpdesk.tickets import service as tickets
from helpdesk.whatsapp.parse import IncomingMessage


def _save(s, client: Client, msg: IncomingMessage, file_path: str | None, *, intake_answer: bool):
    m = Message(
        client_id=client.id,
        direction="in",
        kind=msg.kind,
        text=msg.text,
        file_path=file_path,
        file_name=msg.file_name,
        wa_message_id=msg.wa_message_id,
        intake=intake_answer,
    )
    s.add(m)
    return m


# флуд с одного номера: лишние сообщения сохраняются в заявку, но не рассылаются в Telegram
MAX_PER_MINUTE = 20


async def _flooding(s, client: Client) -> bool:
    recent = await s.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.client_id == client.id,
            Message.direction == "in",
            Message.created_at > clock.utcnow() - timedelta(minutes=1),
        )
    )
    return recent >= MAX_PER_MINUTE


def _no_nul(v: str | None) -> str | None:
    return v.replace("\x00", "") if v else v


async def handle_incoming(s, msg: IncomingMessage, file_path: str | None) -> None:
    # PostgreSQL text не хранит NUL — иначе сообщение терялось бы после повторов
    msg = replace(
        msg,
        text=_no_nul(msg.text),
        file_name=_no_nul(msg.file_name),
        sender_name=_no_nul(msg.sender_name),
    )
    dup = await s.scalar(select(Message.id).where(Message.wa_message_id == msg.wa_message_id))
    if dup is not None:
        return
    client = await s.scalar(select(Client).where(Client.wa_chat_id == msg.chat_id))
    if client is None:
        client = Client(wa_chat_id=msg.chat_id, phone=msg.phone, wa_name=msg.sender_name)
        s.add(client)
        await s.flush()
        _save(s, client, msg, file_path, intake_answer=False)
        intake.start(s, client)
        await s.flush()
        return
    if client.intake_step != "done":
        step = intake.advance(s, client, msg)
        # в заявку не попадают только принятые ответы анкеты (спец. §4.2)
        _save(s, client, msg, file_path, intake_answer=step.consumed)
        if step.done:
            await s.flush()
            await tickets.on_client_message(s, client, None)
        await s.flush()
        return
    flooding = await _flooding(s, client)
    m = _save(s, client, msg, file_path, intake_answer=False)
    await s.flush()
    if msg.kind == "other" and not flooding:
        outbox.wa_text(s, client.wa_chat_id, texts.t(client.lang, "unsupported"))
    await tickets.on_client_message(s, client, m, notify=not flooding)
