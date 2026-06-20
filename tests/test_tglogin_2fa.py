"""Unit tests for the Telegram login flow.

Focused on the 2FA path — specifically the regression where:

* the password prompt stripped whitespace (``str.strip()``), causing
  valid cloud passwords with leading/trailing whitespace to be
  hashed incorrectly and rejected with ``PasswordHashInvalidError``
  even though the user was sure the password was correct;
* ``PasswordHashInvalidError`` was a hard failure that crashed the
  whole CLI on a single mistype, instead of prompting for a retry;
* the env-var fallback path could loop forever on a wrong / unset
  env var because the cap-check pattern used closure identity
  comparison (always ``False``).

The tests run against the real ``TelegramService._interactive_login``
and ``TelegramService.start`` with a mocked Telethon client — no
network access.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import (
    PasswordHashInvalidError,
    SessionPasswordNeededError,
    SrpIdInvalidError,
)
from telethon.tl.types import User as TgUser

from app.config import Settings
from app.telegram_client import TelegramService


# --- helpers -----------------------------------------------------------------


def _build_service(tmp_path, monkeypatch) -> TelegramService:
    """Build a TelegramService backed by a tmp session dir.

    The env-var fallback must not fire during these tests, so we set
    TG_CODE / TG_2FA_PASSWORD explicitly when needed and unset them
    otherwise.
    """
    settings = Settings(
        tg_api_id=12345,
        tg_api_hash="0123456789abcdef0123456789abcdef",
        tg_phone="+15555550100",
        api_key="test-api-key",
        data_dir=str(tmp_path),
        tg_session_name="test",
    )
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    return TelegramService(settings)


def _make_client_mock(*, code_behavior, password_behavior, is_authorized=False):
    """Build a mock TelegramClient.

    ``code_behavior``: async callable (code) -> raises / returns.
    ``password_behavior``: async callable (password) -> raises / returns.
    ``is_authorized``: value returned by ``is_user_authorized()``.
    """
    client = AsyncMock()
    client.connect = AsyncMock(return_value=True)
    client.is_user_authorized = AsyncMock(return_value=is_authorized)
    client.send_code_request = AsyncMock(return_value=True)
    client.disconnect = AsyncMock(return_value=True)
    # ``spec=TgUser`` is what makes ``isinstance(me, User)`` pass in
    # ``TelegramService.start()`` after a successful login.
    me = MagicMock(
        spec=TgUser,
        id=999,
        first_name="Tester",
        username="tester",
        phone="+15555550100",
    )
    client.get_me = AsyncMock(return_value=me)

    async def sign_in(*args, code=None, password=None, **kwargs):
        # Telethon's sign_in is overloaded:
        #   * sign_in(phone, code)         — first-factor OTP
        #   * sign_in(password=password)   — 2FA
        # Both forms are positional for the OTP path. Detect by
        # ``password`` first (kwarg-only), then fall back to the
        # second positional arg as the code.
        if password is not None:
            return await password_behavior(password)
        if len(args) >= 2:
            return await code_behavior(args[1])
        return await code_behavior(code)

    client.sign_in = AsyncMock(side_effect=sign_in)
    return client


class _FakeTelegramClient:
    """Drop-in replacement for ``telethon.TelegramClient`` used by tests.

    Each instance gets a pre-built ``AsyncMock`` whose methods are
    configured by the test. Constructing this never opens a real
    network connection.
    """

    instances: list = []  # type: ignore[type-arg]

    def __init__(self, *args, **kwargs):
        # The test is expected to stash its pre-configured mock on
        # ``self._mock`` before the constructor returns. If it didn't,
        # we create a blank AsyncMock and let the test fill it in.
        if not hasattr(self, "_mock"):
            self._mock = AsyncMock()
        self._mock.connect = AsyncMock(return_value=True)
        self._mock.is_user_authorized = AsyncMock(return_value=False)
        self._mock.send_code_request = AsyncMock(return_value=True)
        self._mock.disconnect = AsyncMock(return_value=True)
        me = MagicMock(
            spec=TgUser,
            id=999,
            first_name="Tester",
            username="tester",
            phone="+15555550100",
        )
        self._mock.get_me = AsyncMock(return_value=me)
        _FakeTelegramClient.instances.append(self)

    def __getattr__(self, name):
        # Sync attribute access. AsyncMock supports sync attribute
        # lookup that returns the mock's own coroutine methods.
        return getattr(self._mock, name)


@pytest.fixture(autouse=True)
def _patch_telegram_client_constructor(monkeypatch):
    """Replace ``TelegramClient`` in the telegram_client module with a
    fake that doesn't try to connect to Telegram. The test sets
    ``svc._pending_mock = ...`` to control the behaviour of the
    instance that will be created when ``start()`` constructs the
    client.
    """
    import app.telegram_client as tc_mod

    monkeypatch.setattr(tc_mod, "TelegramClient", _FakeTelegramClient)
    _FakeTelegramClient.instances = []
    yield


# --- tests -------------------------------------------------------------------


def test_password_prompt_preserves_whitespace(monkeypatch):
    """``_prompt_password`` must not strip — Telegram 2FA passwords may
    legitimately contain leading/trailing whitespace that contributes
    to the hash."""
    from app.cli import _prompt_password

    monkeypatch.setattr(
        "app.cli.getpass.getpass", lambda *a, **kw: "  spaced pass  "
    )
    assert _prompt_password() == "  spaced pass  "


@pytest.mark.asyncio
async def test_2fa_loop_reprompts_on_wrong_password(tmp_path, monkeypatch):
    """A ``PasswordHashInvalidError`` on the first 2FA attempt must
    trigger a re-prompt; the second attempt's password (preserved
    verbatim with whitespace) must be passed through to Telethon."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    password_calls: list[str] = []

    async def password_behavior(password):
        password_calls.append(password)
        if len(password_calls) == 1:
            raise PasswordHashInvalidError(request=None)
        return None  # success

    async def code_behavior(code):
        raise SessionPasswordNeededError(request=None)

    client = _make_client_mock(
        code_behavior=code_behavior,
        password_behavior=password_behavior,
    )
    svc.client = client

    codes = iter(["11111"])
    passwords = iter(["  spaced pass  ", "  spaced pass  "])

    await svc._interactive_login(
        code_prompt=lambda: next(codes),
        password_prompt=lambda: next(passwords),
        code_max_attempts=None,
        password_max_attempts=None,
    )

    assert password_calls == ["  spaced pass  ", "  spaced pass  "]


@pytest.mark.asyncio
async def test_2fa_loop_reprompts_on_srp_id_invalid(tmp_path, monkeypatch):
    """``SrpIdInvalidError`` is a transient error that warrants a retry
    with a fresh ``sign_in`` call, not a new password."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    password_calls: list[str] = []

    async def password_behavior(password):
        password_calls.append(password)
        if len(password_calls) == 1:
            raise SrpIdInvalidError(request=None)
        return None

    async def code_behavior(code):
        raise SessionPasswordNeededError(request=None)

    client = _make_client_mock(
        code_behavior=code_behavior,
        password_behavior=password_behavior,
    )
    svc.client = client

    passwords = iter(["secret", "secret"])

    await svc._interactive_login(
        code_prompt=lambda: "11111",
        password_prompt=lambda: next(passwords),
        code_max_attempts=None,
        password_max_attempts=None,
    )

    assert password_calls == ["secret", "secret"]


@pytest.mark.asyncio
async def test_2fa_loop_caps_at_max_attempts(tmp_path, monkeypatch):
    """The interactive 2FA retry loop must respect ``password_max_attempts``
    when the caller passes a finite cap. (The old env-var path was
    removed; interactive callers can still opt in to a cap by
    passing ``password_max_attempts=...`` to ``start()``.)"""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    password_calls: list[str] = []

    async def password_behavior(password):
        password_calls.append(password)
        raise PasswordHashInvalidError(request=None)

    async def code_behavior(code):
        raise SessionPasswordNeededError(request=None)

    client = _make_client_mock(
        code_behavior=code_behavior,
        password_behavior=password_behavior,
    )
    svc.client = client

    # Build a password iterator that yields the same wrong value
    # forever — the cap must fire before the iterator is exhausted.
    passwords = (f"wrong-{i}" for i in range(1000))

    with pytest.raises(RuntimeError, match="2FA password rejected 3 times"):
        await svc._interactive_login(
            code_prompt=lambda: "11111",
            password_prompt=lambda: next(passwords),
            code_max_attempts=None,
            password_max_attempts=3,
        )

    assert len(password_calls) == 3


@pytest.mark.asyncio
async def test_start_does_not_construct_client_when_no_session(
    tmp_path, monkeypatch
):
    """The lifespan path (``start()`` with no prompts) must NOT
    construct a ``TelegramClient`` when there is no authorized
    session on disk.

    This is the fix for the user-reported "client disconnects
    immediately after login" bug. The pre-fix flow constructed a
    client at lifespan time, generated an auth_key via ``connect()``,
    and then tore it down again when the admin login ran ``start()``
    a second time. The tear-down / reconstruct cycle left Telethon's
    internal sender in an inconsistent state. Skipping client
    construction in the lifespan means exactly one
    ``TelegramClient`` is constructed per login, eliminating the
    race.

    Concretely: after ``start()`` returns in the lifespan path on a
    fresh install, ``svc.client is None`` and ``svc.is_connected``
    is False. The endpoints that need a session translate this
    into a 401 with the actionable hint.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    class _ExplodingTelegramClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "TelegramClient must NOT be constructed during the "
                "lifespan path when there is no authorized session."
            )

    import app.telegram_client as tc_mod
    monkeypatch.setattr(tc_mod, "TelegramClient", _ExplodingTelegramClient)

    # Must not raise — that was the original crash-loop bug.
    await svc.start()

    assert svc.is_connected is False
    assert svc.client is None


@pytest.mark.asyncio
async def test_start_succeeds_when_session_already_authorized(
    tmp_path, monkeypatch
):
    """When a valid session file exists, ``start()`` must connect,
    detect ``is_user_authorized()`` returns True, and set
    ``is_connected=True`` without going through the login flow.

    The session file must contain a non-empty ``auth_key`` row in
    the ``sessions`` table — that's the signal ``start()`` uses to
    decide whether to construct a client in the lifespan path. A
    fresh-install (no file) skips client construction entirely
    (see ``test_start_does_not_construct_client_when_no_session``).
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    # Plant a populated .session file. We don't go through Telethon
    # for this — the helper just looks for a non-empty auth_key
    # blob, which is what a successful previous tglogin would have
    # written.
    session_path = tmp_path / "session" / "test.session"
    import sqlite3
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(
            "CREATE TABLE sessions (auth_key BLOB, user_id INTEGER, "
            "is_authorized INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions (auth_key, user_id, is_authorized) "
            "VALUES (?, ?, ?)",
            (b"\x02" * 256, 999, 1),
        )
        conn.commit()

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=True)
    fake._mock.send_code_request = AsyncMock(
        side_effect=AssertionError("send_code_request must not be called when authorized")
    )
    fake._mock.disconnect = AsyncMock(return_value=True)
    fake._mock.sign_in = AsyncMock(
        side_effect=AssertionError("sign_in must not be called when authorized")
    )
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    await svc.start()

    assert svc.is_connected is True
    assert svc.client is fake


@pytest.mark.asyncio
async def test_client_stays_connected_after_login_completes(
    tmp_path, monkeypatch
):
    """Regression: after a successful ``start(code_prompt=...)`` call
    (the admin login path), ``is_connected`` MUST stay True across
    the boundary where the operator's CLI returns and the next
    request arrives.

    The user-reported symptom was "login reports success, but the
    client disconnects immediately afterward" — that race is closed
    by no longer pre-connecting a client in the lifespan. This test
    pins the invariant: after the admin login returns, the singleton
    is still authorized and the same client is still on disk.
    """
    import asyncio

    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    # First call (login): not authorized yet. After sign_in succeeds,
    # ``is_user_authorized`` will not be called again on this client
    # because we go through the post-login get_me path. (The second
    # ``is_user_authorized`` would only fire on a separate start()
    # call, which we don't make.)
    fake._mock.is_user_authorized = AsyncMock(return_value=False)
    fake._mock.send_code_request = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)
    fake._mock.sign_in = AsyncMock(return_value=None)
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    # 1. Lifespan path: client must NOT be constructed yet.
    await svc.start()
    assert svc.is_connected is False
    assert svc.client is None

    # 2. Admin login: constructs the client, signs in, sets _connected.
    await svc.start(
        code_prompt=lambda: "11111",
        password_prompt=lambda: "secret",
    )
    assert svc.is_connected is True
    assert svc.client is fake
    assert fake._mock.disconnect.call_count == 0, (
        "login completed but the client was disconnected — this is "
        "the user-reported regression."
    )

    # 3. The gap between "background task completes" and "first
    # request arrives": is_connected must still be True and the
    # client must still be on disk.
    await asyncio.sleep(0)
    assert svc.is_connected is True
    assert svc.client is fake
    assert fake._mock.disconnect.call_count == 0


@pytest.mark.asyncio
async def test_start_runs_login_flow_when_caller_passes_prompts(
    tmp_path, monkeypatch
):
    """Regression: ``dnnotification cli tglogin`` (and any caller
    that passes ``code_prompt=``) must ALWAYS enter the login flow,
    even when ``TG_CODE`` is unset. The previous version of the
    disconnected-mode check only looked at the env-var fallback, so
    the CLI's prompts were ignored and ``start()`` returned
    silently with no authentication — exactly the bug that
    prompted this test.

    With the fix, ``send_code_request`` and ``sign_in`` MUST be
    invoked when the caller supplied a ``code_prompt``.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    sign_in_calls: list[str] = []
    send_code_calls = 0

    async def password_behavior(password):
        sign_in_calls.append(f"pwd:{password}")
        return None  # success

    async def code_behavior(code):
        sign_in_calls.append(f"code:{code}")
        return None  # success — no 2FA in this scenario

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=False)

    async def send_code_request(phone):
        nonlocal send_code_calls
        send_code_calls += 1
        return True

    fake._mock.send_code_request = AsyncMock(side_effect=send_code_request)
    fake._mock.disconnect = AsyncMock(return_value=True)

    async def sign_in(*args, code=None, password=None, **kwargs):
        # Telethon's sign_in is overloaded:
        #   * sign_in(phone, code)         — first-factor OTP
        #   * sign_in(password=password)   — 2FA
        # Both forms are positional for the OTP path. Detect by
        # ``password`` first (kwarg-only), then fall back to the
        # second positional arg as the code.
        if password is not None:
            return await password_behavior(password)
        if len(args) >= 2:
            return await code_behavior(args[1])
        return await code_behavior(code)

    fake._mock.sign_in = AsyncMock(side_effect=sign_in)
    me = MagicMock(
        spec=TgUser,
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    # Pass interactive prompts — this is what the CLI does.
    await svc.start(
        code_prompt=lambda: "11111",
        password_prompt=lambda: "secret",
    )

    # The login flow must have actually run.
    assert send_code_calls == 1, "send_code_request was not called"
    assert sign_in_calls == ["code:11111"], (
        f"expected exactly one code sign_in, got {sign_in_calls}"
    )
    assert svc.is_connected is True
