from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from .config import Settings, get_settings
from .logger import get_logger

logger = get_logger("telegram_client")


class TelegramService:
    """
    Manages a single persistent MTProto (Telethon) connection.

    The client is started once at app startup and re-used across requests.
    FloodWaitError is awaited automatically so the rest of the pipeline is
    not interrupted.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Optional[TelegramClient] = None
        self._lock = asyncio.Lock()
        self._connected: bool = False

    async def start(self) -> None:
        """Initialize the client and authenticate (interactive on first run)."""
        async with self._lock:
            if self._connected and self.client is not None:
                return

            logger.info("Initializing Telegram client (session=%s)", self.settings.tg_session_name)
            # Make sure the session directory exists; Telethon will create
            # the .session file inside it on first connect.
            session_path = self.settings.session_path
            session_dir = session_path.parent
            try:
                session_dir.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                # The session dir is bind-mounted from the host. The
                # container runs as root, and the install script creates
                # the host directory as root with mode 755, so a
                # PermissionError here means the bind-mount is not what
                # we expect — for example, the host directory was
                # created by a different user, or its mode has been
                # changed by hand. Surface a clear diagnostic and the
                # host-side fix.
                stat_info = None
                try:
                    stat_info = session_dir.stat()
                except OSError:
                    pass
                uid_gid = (
                    f"uid={stat_info.st_uid} gid={stat_info.st_gid}"
                    if stat_info is not None
                    else "stat failed"
                )
                raise RuntimeError(
                    f"Cannot create session directory {session_dir} ({uid_gid}); "
                    f"the container is running as uid={os.getuid()}. "
                    f"This usually means the bind-mounted host directory is not "
                    f"writable. On the host, run `sudo chmod -R 755 "
                    f"{session_dir}` (or re-run `dnnotification install` "
                    f"to recreate the layout with the correct permissions). "
                    f"Original error: {exc}"
                ) from exc
            self.client = TelegramClient(
                str(session_path),
                self.settings.tg_api_id,
                self.settings.tg_api_hash,
                device_model="TelegramAutomationServer",
                system_version="1.0",
                app_version="1.0.0",
                lang_code="en",
                system_lang_code="en",
            )

            await self.client.connect()

            if not await self.client.is_user_authorized():
                logger.info("No active session found, starting interactive login")
                phone = os.getenv("TG_PHONE") or self.settings.tg_phone
                try:
                    await self.client.send_code_request(phone)
                    code = os.getenv("TG_CODE")
                    if not code:
                        raise RuntimeError(
                            "First-time login requires TG_CODE env var (the code Telegram sent you)."
                        )
                    await self.client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    password = os.getenv("TG_2FA_PASSWORD")
                    if not password:
                        raise RuntimeError(
                            "2FA enabled on account. Set TG_2FA_PASSWORD env var."
                        )
                    await self.client.sign_in(password=password)
                except (PhoneCodeInvalidError, ApiIdInvalidError) as exc:
                    logger.error("Login failed: %s", exc)
                    raise

            me = await self.client.get_me()
            if not isinstance(me, User):
                raise RuntimeError("Authorized session is not a user account")
            logger.info(
                "Logged in as %s (id=%s, phone=%s)",
                getattr(me, "username", None) or me.first_name,
                me.id,
                me.phone,
            )
            self._connected = True

    async def stop(self) -> None:
        async with self._lock:
            if self.client is not None:
                logger.info("Disconnecting Telegram client")
                try:
                    await self.client.disconnect()
                finally:
                    self.client = None
                    self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self.client is not None

    def require_client(self) -> TelegramClient:
        if self.client is None:
            raise RuntimeError("Telegram client not initialized")
        return self.client

    async def safe_call(self, coro_factory, *args, retries: int = 3, **kwargs):
        """
        Run a Telethon coroutine factory with automatic FloodWait handling.
        coro_factory must be a callable that returns a coroutine when called.
        """
        attempt = 0
        while True:
            try:
                return await coro_factory(*args, **kwargs)
            except FloodWaitError as e:
                wait_s = int(e.seconds) + 1
                logger.warning("FloodWaitError: sleeping %s seconds", wait_s)
                await asyncio.sleep(wait_s)
                attempt += 1
                if attempt > retries:
                    raise
            except Exception:
                raise


_singleton: Optional[TelegramService] = None


def get_telegram_service() -> TelegramService:
    global _singleton
    if _singleton is None:
        _singleton = TelegramService(get_settings())
    return _singleton


@asynccontextmanager
async def lifespan_telegram():
    svc = get_telegram_service()
    await svc.start()
    try:
        yield svc
    finally:
        await svc.stop()
