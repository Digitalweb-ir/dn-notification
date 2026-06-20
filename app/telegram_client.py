from __future__ import annotations

import asyncio
import inspect
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

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

# A prompt callback returns `str | None` (None = abort). It MAY be
# either a sync callable (e.g. ``lambda: getpass.getpass(...)`` used by
# the in-container CLI) or an async coroutine function (used by the
# admin login endpoint to block on an HTTP-driven asyncio.Event). The
# type alias is intentionally loose — both shapes are supported by
# ``TelegramService._interactive_login`` which awaits the result.
PromptFn = Callable[[], Union[Optional[str], Awaitable[Optional[str]]]]


async def _resolve_prompt(prompt: PromptFn) -> Optional[str]:
    """Call a prompt that may be sync or async, returning its value.

    The in-container CLI passes sync callables (e.g.
    ``lambda: getpass.getpass(...)``). The admin login endpoint in
    the running FastAPI process passes async callables that block on
    an ``asyncio.Event`` until the operator submits a value via
    HTTP. Both shapes are accepted; the surrounding code in
    ``_interactive_login`` is already async so we can ``await``
    either kind uniformly.
    """
    result = prompt()
    if inspect.isawaitable(result):
        result = await result
    return result


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

    @staticmethod
    def _has_authorized_session_file(session_path: Path) -> bool:
        """Return True iff ``session_path`` exists and contains a
        non-empty ``auth_key`` row.

        Used to short-circuit client construction in the lifespan
        path on a fresh install — there is no point in constructing
        a ``TelegramClient`` and calling ``connect()`` (which would
        generate a fresh auth_key) when the file is missing entirely.
        Construction is still needed in the admin-login path because
        we have prompts and want to drive ``sign_in`` against the
        resulting client; this helper gates the lifespan path only.

        Implemented with a raw ``sqlite3`` query so we never open a
        Telethon session for the check — opening a session as a side
        effect of an "is there a session?" predicate is the kind of
        foot-gun that just bit us.
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

    async def start(
        self,
        *,
        code_prompt: Optional[PromptFn] = None,
        password_prompt: Optional[PromptFn] = None,
        max_attempts: Optional[int] = None,
    ) -> None:
        """Initialize the client and (re-)use the on-disk session.

        Two call sites:

        * **Lifespan** (no prompts): if a valid ``.session`` file with
          a usable auth_key exists, connect and reuse it. Otherwise
          leave the service disconnected — endpoints that need a
          session will return 401, and the operator can run
          ``dnnotification cli tglogin`` to authenticate against the
          running service.
        * **Admin login endpoint** (prompts passed): drive the OTP /
          2FA flow against the same singleton and set
          ``is_connected`` on success.

        ``max_attempts`` caps how many times each prompt is allowed
        to return an invalid value before the call gives up. ``None``
        (the default) means unlimited — the interactive CLI uses this
        so the user can keep typing until they get it right (or hit
        Ctrl-C).

        Why no client is constructed when there is no session
        -----------------------------------------------------
        Earlier versions of this code path always constructed a
        ``TelegramClient`` during the lifespan, even when there was
        no authorized session on disk. ``connect()`` then generated
        an auth_key and persisted it to SQLite. When the admin login
        later ran ``start()`` again, the prior client was torn down
        and a fresh one was constructed against the same file. That
        tear-down / reconstruct cycle left Telethon's internal
        sender in an inconsistent state — the symptom the user
        reported was "login reports success, but the client
        disconnects immediately afterward".

        The fix is structural: do NOT construct a client in the
        lifespan when there is no on-disk session yet. On a clean
        install the singleton starts with ``self.client is None``,
        the lifespan returns without constructing anything, and the
        admin login's first ``start()`` constructs the very first
        client — there is no prior client to tear down, so the
        race is closed.

        Concretely, ``start()`` constructs a ``TelegramClient``
        only when at least one of these is true:
          * a populated ``.session`` file exists on disk (the
            "container restarted after a previous tglogin" path);
          * the caller passed a ``code_prompt`` (the admin login
            path).
        Otherwise (lifespan path on a fresh install), the service
        stays disconnected and no client is constructed.
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

            # Decide whether we are going to construct a client.
            # Construction is justified only when the lifespan path
            # has a populated .session file to reuse, OR the caller
            # passed prompts (the admin login path). On a fresh
            # install with no prompts there is nothing to do — bail
            # out without ever touching ``TelegramClient``.
            has_session = self._has_authorized_session_file(session_path)
            will_login = code_prompt is not None
            if not has_session and not will_login:
                logger.warning(
                    "No Telegram session and no login initiated. "
                    "The service is running in disconnected mode — "
                    "endpoints that require a session will return 401. "
                    "Run `dnnotification cli tglogin` to sign in."
                )
                return

            # Tear down any prior client. Only reached when a fresh
            # login is requested against an already-running service —
            # on a clean lifespan the singleton starts with
            # ``self.client is None`` and this branch is skipped, so
            # no client is ever torn down before a new login.
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:  # noqa: BLE001
                    # The prior client may already be in a bad state
                    # (an earlier login attempt that never completed);
                    # disconnect failures here shouldn't block the
                    # next login attempt.
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
                # Session on disk is already authorized (this is the
                # "container restarted after a previous tglogin" path).
                # Reuse it without going through the OTP flow.
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
                return

            # Session is not authorized. If the caller did not pass
            # prompts we are in the lifespan path on a fresh install —
            # leave the service disconnected and log a clear warning
            # so the operator knows how to sign in. We deliberately do
            # NOT raise: the container would restart-loop and the
            # operator would lose the ability to run
            # ``dnnotification cli tglogin`` against the running
            # container.
            if code_prompt is None:
                logger.warning(
                    "No Telegram session and no login initiated. "
                    "The service is running in disconnected mode — "
                    "endpoints that require a session will return 401. "
                    "Run `dnnotification cli tglogin` to sign in."
                )
                return

            # Otherwise the caller is the admin login endpoint. Drive
            # the interactive OTP / 2FA flow against this very client.
            await self._interactive_login(
                code_prompt=code_prompt,
                password_prompt=password_prompt or (lambda: None),
                code_max_attempts=max_attempts,
                password_max_attempts=max_attempts,
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
        code_max_attempts: Optional[int] = None,
        password_max_attempts: Optional[int] = None,
    ) -> None:
        """Drive the sign-in flow.

        Sends a code request to the phone configured in :attr:`Settings.tg_phone`,
        then asks ``code_prompt`` for the code the user received.
        ``code_max_attempts`` (``None`` = unlimited) bounds how many
        invalid codes we'll tolerate before failing so the operator
        cannot hang the flow forever on a wrong code.

        If the account has 2FA enabled, ``password_prompt`` is consulted
        for the cloud password, with the same retry semantics governed
        by ``password_max_attempts``. Unlike the OTP path, the 2FA
        path is a real retry loop (not a single-shot prompt) so a
        mistyped password doesn't crash the whole CLI run.
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
            code = await _resolve_prompt(code_prompt)
            if code is None or code == "":
                # Empty submission. The interactive CLI passes
                # ``code_max_attempts=None``, so we treat it as a
                # soft retry — don't burn an attempt, just re-prompt.
                # A bounded cap (``code_max_attempts`` is set) lets
                # callers opt in to a hard ceiling for tests / scripted
                # flows; the loop will still spin until the cap fires.
                if code_max_attempts is None:
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
            password = await _resolve_prompt(password_prompt)
            if not password:
                # Empty password. The interactive CLI passes
                # ``password_max_attempts=None``, so the operator hit
                # Enter on an empty getpass prompt to back out —
                # surface an actionable hint instead of looping.
                # A bounded cap (``password_max_attempts`` is set)
                # is used by tests / scripted flows.
                raise RuntimeError(
                    "Account has 2FA enabled. Run `dnnotification cli tglogin` "
                    "to enter the cloud password interactively."
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
