from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    SrpIdInvalidError,
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
        max_attempts: Optional[int] = None,
    ) -> None:
        """Initialize the client and authenticate (interactive on first run).

        ``code_prompt`` and ``password_prompt`` are optional callables
        that return the login code and 2FA password respectively. They
        default to reading ``TG_CODE`` / ``TG_2FA_PASSWORD`` from the
        environment, which preserves the original scripted-login flow.

        ``max_attempts`` caps how many times each prompt is allowed to
        return an invalid value before the call gives up with a clear
        error. ``None`` (the default) means unlimited — the interactive
        CLI uses this so the user can keep typing until they get it
        right (or hit Ctrl-C). The env-var fallback path passes
        :data:`_ENV_PROMPT_MAX_ATTEMPTS` so a stale or wrong env var
        cannot hang the lifespan forever.

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
                # Decide whether we have any way to authenticate. If
                # neither the caller passed prompts nor the env-var
                # fallback is populated, do NOT raise: the FastAPI
                # lifespan would crash, the container would
                # restart-loop, and the operator would lose the
                # ability to run `dnnotification cli tglogin` against
                # the running container. Instead, leave the client
                # connected-but-unauthorized, log a clear warning, and
                # return. The endpoints that need an authorized
                # session (`/search`, `/send-voice`) check
                # ``is_connected`` and return 503 with an actionable
                # message; the operator can sign in at any time
                # without restarting the container.
                env_code = code_prompt is None
                env_pwd = password_prompt is None
                if env_code:
                    code_prompt = _env_prompt("TG_CODE")
                if env_pwd:
                    password_prompt = _env_prompt("TG_2FA_PASSWORD")

                code_env = os.getenv("TG_CODE")
                if code_env in (None, ""):
                    logger.warning(
                        "No Telegram session and no TG_CODE env var set. "
                        "The service is starting in disconnected mode — "
                        "endpoints that require a session will return 503. "
                        "Run `dnnotification cli tglogin` to sign in."
                    )
                    return

                # We have at least the OTP. Try to log in. If the
                # account turns out to have 2FA enabled but the env
                # var is missing, the prompt loop below will raise
                # with a clear hint — which the lifespan used to let
                # crash the container. Catch that specific case and
                # degrade to disconnected mode instead. Other
                # RuntimeErrors (wrong code, wrong password after
                # multiple attempts) are legitimate failures and
                # still propagate.
                try:
                    await self._interactive_login(
                        code_prompt=code_prompt,
                        password_prompt=password_prompt,
                        code_max_attempts=_ENV_PROMPT_MAX_ATTEMPTS if env_code else max_attempts,
                        password_max_attempts=_ENV_PROMPT_MAX_ATTEMPTS if env_pwd else max_attempts,
                    )
                except RuntimeError as exc:
                    if env_pwd and "2FA" in str(exc) and os.getenv("TG_2FA_PASSWORD") in (None, ""):
                        logger.warning(
                            "Account has 2FA enabled but TG_2FA_PASSWORD is not set. "
                            "The service is starting in disconnected mode — "
                            "endpoints that require a session will return 503. "
                            "Run `dnnotification cli tglogin` to sign in."
                        )
                        return
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

    async def _interactive_login(
        self,
        *,
        code_prompt: PromptFn,
        password_prompt: PromptFn,
        code_max_attempts: Optional[int] = None,
        password_max_attempts: Optional[int] = None,
    ) -> None:
        """Drive the sign-in flow.

        Sends a code request to the phone configured in :attr:`Settings.tg_phone`,
        then asks ``code_prompt`` for the code the user received.
        ``code_max_attempts`` (``None`` = unlimited) bounds how many
        invalid codes we'll tolerate before failing — the env-var
        fallback uses a small cap so a stale or wrong env var cannot
        hang the lifespan, while the interactive CLI passes ``None``
        so the user can keep typing until they get it right or Ctrl-C.

        If the account has 2FA enabled, ``password_prompt`` is consulted
        for the cloud password, with the same retry semantics governed by
        ``password_max_attempts``. Unlike the OTP path, the 2FA path is
        a real retry loop (not a single-shot prompt) so a mistyped
        password doesn't crash the whole CLI run.
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
                # Empty submission. Two cases:
                #   * Interactive (code_max_attempts is None): treat as a
                #     soft retry — don't burn an attempt, just re-prompt.
                #   * Env-var fallback: `start()` pre-checks the env var
                #     before we get here, so an empty here is impossible
                #     under normal operation. If we somehow see it (e.g.
                #     the env var was unset between start() and now), the
                #     cap will kick in on the next iteration as the user
                #     keeps hitting `PhoneCodeInvalidError`.
                if code_max_attempts is None:
                    # Interactive path: soft retry, don't burn an attempt.
                    attempt -= 1
                # Env-var path: just let the loop continue. The next
                # iteration will get the same empty value back, which
                # will burn an attempt and the cap will fire after the
                # configured number of tries (typically 3).
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
                if code_max_attempts is not None and attempt >= code_max_attempts:
                    raise RuntimeError(
                        f"Login code rejected {code_max_attempts} times. "
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

        # 2FA path. Like the OTP path above, we loop on rejection so
        # the operator can re-enter the password without re-running
        # the whole flow. The two distinct Telethon errors that can
        # mean "wrong password" are:
        #   * PasswordHashInvalidError — the SRP handshake completed
        #     but Telegram rejected the resulting password hash.
        #   * SrpIdInvalidError — Telethon sent a stale/duplicate SRP
        #     ID to Telegram (a transient error that warrants a retry
        #     with a fresh client.sign_in() call, not a new password).
        #
        # CRITICAL: the password is passed through verbatim — no
        # stripping — because Telegram 2FA cloud passwords are
        # user-defined and may legitimately contain leading or
        # trailing whitespace that contributes to the hash.
        pwd_attempt = 0
        while True:
            pwd_attempt += 1
            password = password_prompt()
            if not password:
                # Empty password. Two cases:
                #   * Interactive (password_max_attempts is None): the
                #     user hit Enter on an empty getpass prompt to back
                #     out — abort with the actionable hint.
                #   * Env-var (password_max_attempts is set): the env
                #     var was unset/empty; we deferred this check from
                #     start() because the password is only needed when
                #     2FA is actually enabled. Fail loudly here so the
                #     operator knows what to set.
                raise RuntimeError(
                    "Account has 2FA enabled. Run `dnnotification cli tglogin` "
                    "to enter the cloud password interactively, or set "
                    "TG_2FA_PASSWORD in the container env."
                )
            try:
                await self.client.sign_in(password=password)
                return
            except (PasswordHashInvalidError, SrpIdInvalidError) as exc:
                logger.warning(
                    "2FA password was rejected (attempt %d, %s). Try again — "
                    "the cloud password is case-sensitive and whitespace "
                    "matters; do not trim it.",
                    pwd_attempt,
                    type(exc).__name__,
                )
                if (
                    password_max_attempts is not None
                    and pwd_attempt >= password_max_attempts
                ):
                    raise RuntimeError(
                        f"2FA password rejected {password_max_attempts} times. "
                        f"Aborting; re-run `dnnotification cli tglogin`."
                    ) from exc
                # Loop and re-prompt.

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
