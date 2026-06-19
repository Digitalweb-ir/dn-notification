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
    me = MagicMock(
        id=999,
        first_name="Tester",
        username="tester",
        phone="+15555550100",
    )
    client.get_me = AsyncMock(return_value=me)

    async def sign_in(*args, code=None, password=None, **kwargs):
        if password is not None:
            return await password_behavior(password)
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
async def test_2fa_env_path_caps_at_max_attempts(tmp_path, monkeypatch):
    """Env-var fallback must cap the 2FA retry loop so a wrong env var
    can't hang the lifespan. This is the regression that the broken
    ``is``-closure cap check failed to catch."""
    monkeypatch.setenv("TG_CODE", "11111")
    monkeypatch.setenv("TG_2FA_PASSWORD", "wrong")
    svc = _build_service(tmp_path, monkeypatch)

    # Pre-stage the mock that the FakeTelegramClient constructor will
    # install when ``start()`` instantiates it.
    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()

    async def password_behavior(password):
        raise PasswordHashInvalidError(request=None)

    async def code_behavior(code):
        raise SessionPasswordNeededError(request=None)

    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=False)
    fake._mock.send_code_request = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)

    async def sign_in(*args, code=None, password=None, **kwargs):
        if password is not None:
            return await password_behavior(password)
        return await code_behavior(code)

    fake._mock.sign_in = AsyncMock(side_effect=sign_in)
    me = MagicMock(
        id=999, first_name="Tester", username="tester", phone="+15555550100"
    )
    fake._mock.get_me = AsyncMock(return_value=me)

    # Wire the fake into the constructor: stash a queue of one fake
    # that the constructor will pop and return.
    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    with pytest.raises(RuntimeError, match="2FA password rejected 3 times"):
        await svc.start()


@pytest.mark.asyncio
async def test_start_env_path_raises_when_TG_CODE_unset(tmp_path, monkeypatch):
    """``start()`` must fail loudly when TG_CODE is unset, rather
    than entering the login loop and spinning forever."""
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path, monkeypatch)

    fake = _FakeTelegramClient.__new__(_FakeTelegramClient)
    fake._mock = AsyncMock()
    fake._mock.connect = AsyncMock(return_value=True)
    fake._mock.is_user_authorized = AsyncMock(return_value=False)
    fake._mock.send_code_request = AsyncMock(return_value=True)
    fake._mock.disconnect = AsyncMock(return_value=True)
    fake._mock.sign_in = AsyncMock(
        side_effect=AssertionError("sign_in should not be called when TG_CODE unset")
    )

    import app.telegram_client as tc_mod
    instances = [fake]

    class _Queue:
        def __call__(self, *args, **kwargs):
            return instances.pop(0)

    monkeypatch.setattr(tc_mod, "TelegramClient", _Queue())

    with pytest.raises(RuntimeError, match="First-time login required"):
        await svc.start()
