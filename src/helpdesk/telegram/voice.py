from dataclasses import dataclass

from helpdesk.telegram.media import Content, extract_content


@dataclass
class VoiceResult:
    status: str  # ok | failed | too_long | disabled
    content: Content | None
    text: str | None
    progress: object | None  # сообщение «⏳ …», вызывающий его редактирует


def duration(message) -> int:
    obj = message.voice or message.audio
    return (obj.duration or 0) if obj else 0


async def recognize(message, bot, settings, stt, *, keep_file: bool) -> VoiceResult:
    """keep_file=True — файл нужен и без расшифровки (комментарий уходит аудио)."""
    too_long = duration(message) > settings.stt_max_seconds
    if too_long or not stt.enabled:
        status = "too_long" if too_long else "disabled"
        content = await extract_content(bot, message, settings.files_dir) if keep_file else None
        return VoiceResult(status, content, None, None)
    content = await extract_content(bot, message, settings.files_dir)
    note = "⏳ Распознаю…" if stt.is_loaded else "⏳ Загружаю модель распознавания (один раз)…"
    progress = await message.answer(note)
    text = await stt.transcribe(content.file_path)
    return VoiceResult("ok" if text else "failed", content, text, progress)
