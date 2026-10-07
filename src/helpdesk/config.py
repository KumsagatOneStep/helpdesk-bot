from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    admin_tg_ids: str = ""
    green_api_url: str = "https://api.green-api.com"
    green_api_media_url: str = ""
    green_api_id_instance: str
    green_api_token: str
    database_url: str
    tz: str = "Asia/Almaty"
    remind_after_min: int = 10
    files_dir: str = "/data/files"

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_tg_ids.replace(" ", "").split(",") if x}

    @property
    def media_url(self) -> str:
        return (self.green_api_media_url or self.green_api_url).rstrip("/")
