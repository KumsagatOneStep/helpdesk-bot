from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup


def markup(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in buttons
        ]
    )


class AiogramSender:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id, *, text, file_path, file_kind, buttons) -> int:
        kb = markup(buttons)
        if not file_path:
            m = await self.bot.send_message(chat_id, (text or "")[:4096], reply_markup=kb)
            return m.message_id
        caption = text[:1024] if text else None
        if file_kind == "photo":
            m = await self.bot.send_photo(
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
