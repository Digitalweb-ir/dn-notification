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
    debug: bool = False

    @property
    def log_level(self) -> str:
        return "DEBUG" if self.debug else "INFO"

    # Security
    api_key: str

    # Search
    search_top_matches: int = 3
    search_cache_ttl: int = 300

    # Persistent storage — DATA_DIR is the only path the user configures.
    # The voices, session, and logs sub-directories are derived from it
    # so there's exactly one source of truth. The host bind-mount in
    # docker-compose.yaml maps DATA_DIR on the host onto the same path
    # inside the container.
    data_dir: str = "/var/lib/dn-notification"

    @property
    def voices_dir(self) -> str:
        return f"{self.data_dir}/voices"

    @property
    def session_dir(self) -> str:
        return f"{self.data_dir}/session"

    @property
    def logs_dir(self) -> str:
        return f"{self.data_dir}/logs"

    @property
    def session_path(self) -> Path:
        return Path(self.session_dir) / f"{self.tg_session_name}.session"

    @property
    def voices_path(self) -> Path:
        # The directory is created and owned by docker-entrypoint.sh.
        # The app only needs the path; if it is missing, something is
        # wrong with the entrypoint (or the image was started without
        # it), and the right fix is at deploy time, not at request time.
        return Path(self.voices_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
