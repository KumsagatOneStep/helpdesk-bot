import uuid
from datetime import datetime
from pathlib import Path


def new_file_path(files_dir: str, file_name: str | None, now: datetime) -> Path:
    suffix = Path(file_name).suffix.lower() if file_name else ""
    if len(suffix) > 10:
        suffix = ""
    d = Path(files_dir) / f"{now:%Y}" / f"{now:%m}"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{uuid.uuid4().hex}{suffix}"
