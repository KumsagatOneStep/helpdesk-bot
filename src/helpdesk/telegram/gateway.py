import os
from functools import wraps

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from helpdesk.delivery.worker import PermanentSendError

MB = 1024 * 1024
MAX_UPLOAD = 50 * MB  # лимит загрузки файлов ботом
MAX_PHOTO = 10 * MB  # больше — только документом


def markup(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in buttons
        ]
    )


def _permanent(fn):
    @wraps(fn)
    async def wrapper(*args, **kw):
        try:
            return await fn(*args, **kw)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramEntityTooLarge) as e:
            raise PermanentSendError(str(e)) from e

    return wrapper


class AiogramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    @_permanent
    async def send(self, chat_id, *, text, file_path, file_kind, buttons) -> int:
        kb = markup(buttons)
        size = os.path.getsize(file_path) if file_path else 0
        if size > MAX_UPLOAD:
            note = f"[файл {size // MB} МБ — слишком большой для Telegram]"
            text, file_path = f"{note}\n{text}" if text else note, None
        if not file_path:
            m = await self.bot.send_message(chat_id, (text or "")[:4096], reply_markup=kb)
            return m.message_id
        caption = text[:1024] if text else None
        if file_kind == "photo" and size > MAX_PHOTO:
            file_kind = "document"
        if file_kind == "photo":
            try:
                m = await self.bot.send_photo(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
            except TelegramBadRequest:  # Telegram не принял как фото — отправим файлом
                m = await self.bot.send_document(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
        elif file_kind == "video":
            m = await self.bot.send_video(
                chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
            )
        elif file_kind == "voice":
            try:
                m = await self.bot.send_voice(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
            except TelegramBadRequest:  # не OGG/MP3/M4A — отправим файлом
                m = await self.bot.send_document(
                    chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
                )
        else:
            m = await self.bot.send_document(
                chat_id, FSInputFile(file_path), caption=caption, reply_markup=kb
            )
        return m.message_id

    @_permanent
    async def edit(self, chat_id, message_id, text, buttons) -> None:
        try:
            await self.bot.edit_message_text(
                text=text[:4096],
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup(buttons),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                raise

    async def delete(self, chat_id, message_id) -> None:
        await self.bot.delete_message(chat_id, message_id)
