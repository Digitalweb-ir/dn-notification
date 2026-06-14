from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram credentials
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session_name: str = "telegram_session"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Security
    api_key: str

    # Search
    search_limit_per_chat: int = 200
    search_top_matches: int = 3
    search_cache_ttl: int = 300

    # Persistent storage — defaults match the in-container layout under
    # /var/lib/dn-notification, which is bind-mounted from the same path
    # on the host (see docker-compose.yaml). Override when running uvicorn
    # directly outside of compose.
    data_dir: str = "/var/lib/dn-notification"
    voices_dir: str = "/var/lib/dn-notification/voices"
    session_dir: str = "/var/lib/dn-notification/session"
    logs_dir: str = "/var/lib/dn-notification/logs"

    @property
    def session_path(self) -> Path:
        return Path(self.session_dir) / f"{self.tg_session_name}.session"

    @property
    def voices_path(self) -> Path:
        p = Path(self.voices_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
