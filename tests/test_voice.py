from pathlib import Path
from types import SimpleNamespace

import pytest

from helpdesk.telegram import voice
from helpdesk.telegram.media import FileTooBig
from tests.fakes import FakeTranscriber


class Progress:
    def __init__(self, text):
        self.text = text
        self.edits = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class VoiceMsg:
    def __init__(self, duration=5, size=10):
        self.text = None
        self.caption = None
        for k in ("photo", "document", "audio", "video", "video_note"):
            setattr(self, k, None)
        self.voice = SimpleNamespace(duration=duration, file_size=size, file_unique_id="v")
        self.sent = []

    async def answer(self, text, **kw):
        p = Progress(text)
        self.sent.append(p)
        return p


class DlBot:
    def __init__(self):
        self.downloads = 0

    async def download(self, obj, destination):
        self.downloads += 1
        Path(destination).write_bytes(b"ogg")


def _settings(tmp_path, max_seconds=120):
    return SimpleNamespace(files_dir=str(tmp_path), stt_max_seconds=max_seconds)


async def test_ok(tmp_path, clock_):
    m, bot, stt = VoiceMsg(), DlBot(), FakeTranscriber("Заменить картридж")
    r = await voice.recognize(m, bot, _settings(tmp_path), stt, keep_file=False)
    assert (r.status, r.text) == ("ok", "Заменить картридж")
    assert r.content.kind == "audio" and r.content.file_name == "voice.ogg"
    assert stt.calls == [r.content.file_path]
    assert [p.text for p in m.sent] == ["⏳ Распознаю…"] and r.progress is m.sent[0]


async def test_first_call_warns_about_model_download(tmp_path, clock_):
    m = VoiceMsg()
    await voice.recognize(
        m, DlBot(), _settings(tmp_path), FakeTranscriber("x", loaded=False), keep_file=False
    )
    assert m.sent[0].text == "⏳ Загружаю модель распознавания (один раз)…"


async def test_failed(tmp_path, clock_):
    r = await voice.recognize(
        VoiceMsg(), DlBot(), _settings(tmp_path), FakeTranscriber(None), keep_file=False
    )
    assert (r.status, r.text) == ("failed", None) and r.content is not None


async def test_too_long_not_downloaded_in_dialog(tmp_path, clock_):
    m, bot, stt = VoiceMsg(duration=121), DlBot(), FakeTranscriber("x")
    r = await voice.recognize(m, bot, _settings(tmp_path), stt, keep_file=False)
    assert (r.status, r.content, r.progress) == ("too_long", None, None)
    assert bot.downloads == 0 and stt.calls == [] and m.sent == []


async def test_too_long_downloaded_for_comment(tmp_path, clock_):
    bot, stt = DlBot(), FakeTranscriber("x")
    r = await voice.recognize(VoiceMsg(duration=500), bot, _settings(tmp_path), stt, keep_file=True)
    assert r.status == "too_long" and r.content is not None
    assert bot.downloads == 1 and stt.calls == []


async def test_disabled(tmp_path, clock_):
    bot = DlBot()
    r = await voice.recognize(
        VoiceMsg(), bot, _settings(tmp_path), FakeTranscriber("x", enabled=False), keep_file=False
    )
    assert (r.status, r.content) == ("disabled", None) and bot.downloads == 0


async def test_file_too_big_propagates(tmp_path, clock_):
    with pytest.raises(FileTooBig):
        await voice.recognize(
            VoiceMsg(size=30 * 1024 * 1024),
            DlBot(),
            _settings(tmp_path),
            FakeTranscriber("x"),
            keep_file=False,
        )


def test_duration_reads_voice_or_audio():
    m = VoiceMsg(duration=7)
    assert voice.duration(m) == 7
    m.voice, m.audio = None, SimpleNamespace(duration=None)
    assert voice.duration(m) == 0
