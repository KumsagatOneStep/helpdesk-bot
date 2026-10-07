from datetime import UTC, datetime

from helpdesk import clock
from helpdesk.config import Settings

BASE = dict(
    telegram_bot_token="t",
    green_api_id_instance="1101",
    green_api_token="g",
    database_url="postgresql+asyncpg://x/y",
)


def test_admin_ids_parsed_and_media_url_defaults():
    s = Settings(_env_file=None, admin_tg_ids="111, 222,", **BASE)
    assert s.admin_ids == {111, 222}
    assert s.media_url == "https://api.green-api.com"
    assert s.remind_after_min == 10


def test_media_url_override():
    s = Settings(_env_file=None, green_api_media_url="https://7103.media.green-api.com/", **BASE)
    assert s.media_url == "https://7103.media.green-api.com"


def test_local_time_uses_configured_zone():
    clock.set_local_tz("Asia/Almaty")
    dt = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    assert clock.local(dt).utcoffset().total_seconds() == 5 * 3600
    assert clock.utcnow().tzinfo is not None
