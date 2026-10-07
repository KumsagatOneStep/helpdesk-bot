from dataclasses import dataclass

from helpdesk import clock, files

MAX_BYTES = 20 * 1024 * 1024


class FileTooBig(Exception):
    pass


@dataclass
class Content:
    kind: str
    text: str | None
    file_path: str | None
    file_name: str | None


def _pick(message):
    if message.photo:
        p = message.photo[-1]
        return "image", p, f"photo_{p.file_unique_id}.jpg"
    if message.document:
        return "document", message.document, message.document.file_name or "file"
    if message.voice:
        return "audio", message.voice, "voice.ogg"
    if message.audio:
        return "audio", message.audio, message.audio.file_name or "audio.mp3"
    if message.video:
        return "video", message.video, message.video.file_name or "video.mp4"
    if message.video_note:
        return "video", message.video_note, "video_note.mp4"
    return None


async def extract_content(bot, message, files_dir: str) -> Content:
    if message.text is not None:
        return Content("text", message.text, None, None)
    picked = _pick(message)
    if picked is None:
        raise ValueError("unsupported content")
    kind, obj, name = picked
    if (obj.file_size or 0) > MAX_BYTES:
        raise FileTooBig(name)
    path = files.new_file_path(files_dir, name, clock.utcnow())
    await bot.download(obj, destination=path)
    return Content(kind, message.caption, str(path), name)
