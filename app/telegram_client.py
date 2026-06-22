"""telegram_client — singleton TelegramService that manages one Telethon client.

The service is started once at app startup (lifespan) and re-used across
requests. It only connects when a session file already exists on disk.
New sessions are created by the operator via ``python -m app.cli tglogin``,
which runs in a separate process and writes the session file. The next API
request triggers ``ensure_connected()``, which detects the new session file
and reconnects automatically.

FloodWaitError is awaited automatically so the rest of the pipeline is
not interrupted.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError
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

    Login is performed by the CLI (``python -m app.cli tglogin``) in a
    separate process. After the CLI saves the session file, the next API
    request triggers ``ensure_connected()``, which builds a TelegramClient
    from the on-disk session and reconnects.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Optional[TelegramClient] = None
        self._lock = asyncio.Lock()
        self._connected: bool = False

    @staticmethod
    def _has_authorized_session_file(session_path: Path) -> bool:
        """Return True iff ``session_path`` exists and contains a
        non-empty ``auth_key`` row.

        Used to decide whether to construct a TelegramClient in the
        lifespan path. When the file is absent (fresh install), no
        client is constructed — the service stays disconnected until
        the operator runs ``tglogin``.

        Implemented with a raw ``sqlite3`` query so we never open a
        Telethon session for the check — opening a session as a side
        effect of an "is there a session?" predicate is a foot-gun.
        """
        if not session_path.exists():
            return False
        try:
            import sqlite3

            with sqlite3.connect(str(session_path)) as conn:
                cursor = conn.execute(
                    "SELECT auth_key FROM sessions LIMIT 1"
                )
                row = cursor.fetchone()
            return bool(row and row[0] and len(row[0]) > 0)
        except Exception:  # noqa: BLE001
            # A corrupt / non-SQLite file at the session path is not
            # our problem to diagnose here — fall through to "treat as
            # no session" and let ``connect()`` regenerate if needed.
            return False

    async def start(self) -> None:
        """Initialize the client and reuse the on-disk session.

        Called from the FastAPI lifespan. If a valid ``.session`` file
        with a usable auth_key exists, connect and reuse it. Otherwise
        leave the service disconnected — endpoints that need a session
        will return 401, and the operator can run ``tglogin`` to
        authenticate. The next API request triggers
        ``ensure_connected()`` which will pick up the new session file.

        No TelegramClient is constructed when there is no session file
        on disk. This is critical: if ``connect()`` were called without
        a prior session, Telethon would generate a fresh auth_key and
        persist it to SQLite. A subsequent ``ensure_connected()`` would
        find the file, construct a new client, and the old ``connect()``
        state would be stale — exactly the bug that caused
        "login reports success, but the client disconnects immediately
        afterward." By never constructing a client until a real session
        exists, there is only ever one client per service lifetime.
        """
        async with self._lock:
            if self._connected and self.client is not None:
                return

            session_path = self.settings.session_path
            session_dir = session_path.parent
            if not session_dir.is_dir():
                raise RuntimeError(
                    f"Session directory {session_dir} is missing. "
                    f"docker-entrypoint.sh is responsible for creating it; "
                    f"check the container logs for the [entrypoint] output."
                )
            if not os.access(session_dir, os.W_OK):
                raise RuntimeError(
                    f"Session directory {session_dir} is not writable by "
                    f"uid={os.getuid()}. docker-entrypoint.sh should have "
                    f"chowned it; if you are overriding USER in compose, "
                    f"remove that override or set it to 0."
                )

            has_session = self._has_authorized_session_file(session_path)
            if not has_session:
                logger.warning(
                    "No Telegram session on disk. "
                    "The service is running in disconnected mode — "
                    "endpoints that require a session will return 401. "
                    "Run `dnnotification cli tglogin` to sign in."
                )
                return

            # Tear down any prior client. This is only reached if a
            # session file appeared on disk after the initial lifespan
            # (e.g. the operator just ran tglogin). On a fresh lifespan
            # the singleton starts with ``self.client is None`` so this
            # branch is skipped.
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self.client = None
                self._connected = False

            logger.info("Initializing Telegram client (session=%s)", self.settings.tg_session_name)
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

            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                if not isinstance(me, User):
                    raise RuntimeError("Authorized session is not a user account")
                logger.info(
                    "Reusing existing session for %s (id=%s, phone=%s)",
                    getattr(me, "username", None) or me.first_name,
                    me.id,
                    me.phone,
                )
                self._connected = True
            else:
                # Session file exists but is not authorized (e.g. the
                # CLI was killed mid-login before sign_in completed).
                logger.warning(
                    "Session file exists but is not authorized. "
                    "Run `dnnotification cli tglogin` to sign in."
                )

    async def ensure_connected(self) -> bool:
        """Try to (re-)connect if the service is not connected.

        Called by ``_require_telegram_session`` before returning 401.
        If a session file appeared on disk (e.g. after the CLI's
        ``tglogin`` wrote one), this will construct a TelegramClient
        and connect using it. Returns True if the service is now
        connected (or was already), False if no session is available.
        """
        if self.is_connected:
            return True
        session_path = self.settings.session_path
        if not self._has_authorized_session_file(session_path):
            return False
        try:
            await self.start()
        except Exception:
            logger.exception("Auto-reconnect failed")
            return False
        return self.is_connected

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
