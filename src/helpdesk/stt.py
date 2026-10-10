import asyncio
import os
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()


class Transcriber(Protocol):
    enabled: bool
    is_loaded: bool

    async def transcribe(self, path: str) -> str | None: ...


class DisabledTranscriber:
    enabled = False
    is_loaded = True

    async def transcribe(self, path: str) -> str | None:
        return None


class WhisperTranscriber:
    """Локальный Whisper: модель грузится при первом голосовом, распознаём по одному."""

    enabled = True

    def __init__(self, model_name: str, download_root: str):
        self.model_name = model_name
        self.download_root = download_root
        self._model = None
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        # у пользователя контейнера нет домашней папки — кэш HF держим рядом с моделями
        os.environ.setdefault("HF_HOME", self.download_root)
        from faster_whisper import WhisperModel  # тяжёлый импорт, нужен только здесь

        return WhisperModel(
            self.model_name, device="cpu", compute_type="int8", download_root=self.download_root
        )

    def _run(self, path: str) -> str:
        if self._model is None:
            self._model = self._load()
        segments, _info = self._model.transcribe(path, vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, path: str) -> str | None:
        async with self._lock:
            try:
                text = await asyncio.to_thread(self._run, path)
            except Exception:
                log.exception("stt failed")
                return None
        return text or None


def make_transcriber(settings) -> Transcriber:
    if not settings.stt_enabled:
        return DisabledTranscriber()
    root = str(Path(settings.files_dir).parent / "models")
    return WhisperTranscriber(settings.stt_model, root)
