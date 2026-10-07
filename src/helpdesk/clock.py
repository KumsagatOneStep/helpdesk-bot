from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Almaty")


def utcnow() -> datetime:
    return datetime.now(UTC)


def set_local_tz(name: str) -> None:
    global LOCAL_TZ
    LOCAL_TZ = ZoneInfo(name)


def local(dt: datetime) -> datetime:
    return dt.astimezone(LOCAL_TZ)
