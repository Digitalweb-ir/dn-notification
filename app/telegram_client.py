from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from .config import Settings, get_settings
from .logger import get_logger

logger = get_logger("telegram_client")

# Default cap on the number of times the user is re-prompted for the same
# code/password when TG_CODE / TG_2FA_PASSWORD are provided as env vars.
# The interactive CLI ignores this — it keeps prompting until the user
# gets it right or aborts with Ctrl-C.
_ENV_PROMPT_MAX_ATTEMPTS = 3

# A prompt callback is `() -> str | None`. Return None to abort.
PromptFn = Callable[[], Optional[str]]


def _env_prompt(env_var: str) -> PromptFn:
    """Build a single-shot prompt that reads once from an env var."""
    def _prompt() -> Optional[str]:
        return os.getenv(env_var)
    return _prompt


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

    async def start(
        self,
        *,
        code_prompt: Optional[PromptFn] = None,
        password_prompt: Optional[PromptFn] = None,
    ) -> None:
        """Initialize the client and authenticate (interactive on first run).

        ``code_prompt`` and ``password_prompt`` are optional callables
        that return the login code and 2FA password respectively. They
        default to reading ``TG_CODE`` / ``TG_2FA_PASSWORD`` from the
        environment, which preserves the original scripted-login flow.

        The interactive CLI (``python -m app.cli tglogin``) passes callbacks
        built on top of :func:`getpass.getpass` so the code is never
        echoed to the terminal and is never written to disk.
        """
        async with self._lock:
            if self._connected and self.client is not None:
                return

            logger.info("Initializing Telegram client (session=%s)", self.settings.tg_session_name)
            # The session directory is created and owned by
            # docker-entrypoint.sh, so by the time we get here it must
            # already exist and be writable. We do a fast sanity check
            # only — never try to create or chown it from inside the
            # app: a Python process running as root can do it, but
            # doing it from the entrypoint means the layout is correct
            # *before* the app starts, and the operator gets a single,
            # clear error from the entrypoint logs rather than a stack
            # trace from the app.
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
                # Caller did not pass prompts -> fall back to the env-var
                # path. This is what the lifespan uses: it cannot prompt
                # interactively, so the only way to log in is via
                # TG_CODE/TG_2FA_PASSWORD. The error message points the
                # operator at the proper fix: run
                # `dnnotification cli tglogin`.
                if code_prompt is None:
                    code_prompt = _env_prompt("TG_CODE")
                if password_prompt is None:
                    password_prompt = _env_prompt("TG_2FA_PASSWORD")
                await self._interactive_login(
                    code_prompt=code_prompt,
                    password_prompt=password_prompt,
                )

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

    async def _interactive_login(
        self,
        *,
        code_prompt: PromptFn,
        password_prompt: PromptFn,
    ) -> None:
        """Drive the sign-in flow.

        Sends a code request to the phone configured in :attr:`Settings.tg_phone`,
        then asks ``code_prompt`` for the code the user received. Re-prompts
        on ``PhoneCodeInvalidError`` (no resend) up to
        :data:`_ENV_PROMPT_MAX_ATTEMPTS` times — but only the env-var path
        actually hits that cap; interactive prompts keep going until the
        user gets it right or aborts.

        If the account has 2FA enabled, ``password_prompt`` is consulted for
        the cloud password.
        """
        assert self.client is not None  # set by start()
        phone = self.settings.tg_phone

        try:
            await self.client.send_code_request(phone)
        except ApiIdInvalidError as exc:
            # The credentials themselves are wrong — re-prompting won't
            # help, so fail fast.
            raise RuntimeError(
                f"Telegram rejected TG_API_ID / TG_API_HASH ({exc}). "
                f"Check the values from https://my.telegram.org/apps."
            ) from exc

        attempt = 0
        while True:
            attempt += 1
            code = code_prompt()
            if code is None or code == "":
                # Distinguish "no env var set" (scripted path) from
                # "user hit Enter on an empty prompt" (interactive).
                # Both are fatal for the env-var path; interactive
                # prompts are looped by the caller until the user types
                # something or Ctrl-Cs.
                if os.getenv("TG_CODE") is None and code_prompt is _env_prompt("TG_CODE"):
                    raise RuntimeError(
                        "First-time login required. Run `dnnotification cli tglogin` "
                        "from the host (or, for scripted login, set TG_CODE and "
                        "TG_2FA_PASSWORD in the container env)."
                    )
                # Interactive: an empty submission is a soft retry, not
                # an abort — but don't burn an attempt on it.
                attempt -= 1
                continue

            try:
                await self.client.sign_in(phone, code)
                return
            except SessionPasswordNeededError:
                # Code was correct; account has 2FA enabled. Fall through
                # to the password prompt below.
                break
            except PhoneCodeInvalidError:
                logger.warning(
                    "Login code was rejected (attempt %d). Try again — "
                    "the previously-issued code remains valid until it "
                    "expires; we are NOT requesting a new SMS.",
                    attempt,
                )
                if attempt >= _ENV_PROMPT_MAX_ATTEMPTS and code_prompt is _env_prompt("TG_CODE"):
                    raise RuntimeError(
                        f"Login code rejected {_ENV_PROMPT_MAX_ATTEMPTS} times. "
                        f"Aborting; re-run `dnnotification cli tglogin`."
                    )
                # Loop and re-prompt.
                continue
            except PhoneCodeExpiredError as exc:
                # The previously-sent code is no longer valid; the user
                # must request a new one. Re-send and re-prompt.
                logger.warning("Login code expired; requesting a new one.")
                await self.client.send_code_request(phone)
                continue
            except ApiIdInvalidError as exc:
                raise RuntimeError(
                    f"Telegram rejected TG_API_ID / TG_API_HASH ({exc})."
                ) from exc

        # 2FA path.
        password = password_prompt()
        if not password:
            if os.getenv("TG_2FA_PASSWORD") is None and password_prompt is _env_prompt("TG_2FA_PASSWORD"):
                raise RuntimeError(
                    "Account has 2FA enabled. Run `dnnotification cli tglogin` "
                    "to enter the cloud password interactively, or set "
                    "TG_2FA_PASSWORD in the container env."
                )
            raise RuntimeError("2FA password is required.")
        await self.client.sign_in(password=password)

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
