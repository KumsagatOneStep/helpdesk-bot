from types import SimpleNamespace

from helpdesk import stt


class FakeModel:
    def __init__(self, texts=(), error=None):
        self.texts = texts
        self.error = error
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append((path, kw))
        if self.error:
            raise self.error
        return iter(SimpleNamespace(text=t) for t in self.texts), SimpleNamespace()


def _whisper(monkeypatch, model):
    loads = []

    def load(self):
        loads.append(1)
        return model

    monkeypatch.setattr(stt.WhisperTranscriber, "_load", load)
    return stt.WhisperTranscriber("small", "/tmp/models"), loads


async def test_disabled_transcriber():
    d = stt.DisabledTranscriber()
    assert (d.enabled, d.is_loaded) == (False, True)
    assert await d.transcribe("/x.ogg") is None


async def test_whisper_joins_segments_and_loads_once(monkeypatch):
    w, loads = _whisper(monkeypatch, FakeModel([" Заменить картридж", " в 214 "]))
    assert w.enabled and not w.is_loaded
    assert await w.transcribe("/a.ogg") == "Заменить картридж в 214"
    assert w.is_loaded
    assert await w.transcribe("/b.ogg") == "Заменить картридж в 214"
    assert loads == [1]


async def test_whisper_empty_result_is_none(monkeypatch):
    w, _ = _whisper(monkeypatch, FakeModel(["  ", ""]))
    assert await w.transcribe("/a.ogg") is None


async def test_whisper_exception_returns_none(monkeypatch):
    w, _ = _whisper(monkeypatch, FakeModel(error=RuntimeError("bad audio")))
    assert await w.transcribe("/a.ogg") is None


def test_make_transcriber(tmp_path):
    on = SimpleNamespace(stt_enabled=True, stt_model="base", files_dir=str(tmp_path / "files"))
    w = stt.make_transcriber(on)
    assert isinstance(w, stt.WhisperTranscriber)
    assert (w.model_name, w.download_root) == ("base", str(tmp_path / "models"))
    off = SimpleNamespace(stt_enabled=False, stt_model="base", files_dir="/data/files")
    assert isinstance(stt.make_transcriber(off), stt.DisabledTranscriber)
