"""Tests for session persistence after a successful Telegram login.

The original regression was reported as ``dnnotification cli tglogin``
appears to succeed but the .session file is "not saved". Investigation
showed the SQLite session file IS written correctly during
``TelegramService.start()`` — Telethon's ``_auth_key_callback`` writes
the auth_key into the SQLite DB the moment the MTProto sender
generates it (which happens at ``connect()`` time, before ``sign_in``).
What was actually missing on the operator's side was a host-side
``cmd_login`` wrapper that restarts the container after a successful
login so the long-running service picks up the new auth_key.

These tests pin down the persistence behaviour itself: after a
successful ``tglogin``, the .session file on disk MUST contain a
non-empty auth_key, and a fresh ``TelegramClient`` constructed from
the same path MUST report ``is_user_authorized() == True`` without
any login prompt.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.crypto import AuthKey
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User as TgUser

from app.config import Settings
from app.telegram_client import TelegramService


# --- helpers -----------------------------------------------------------------


def _build_service(tmp_path) -> TelegramService:
    """Build a TelegramService with a tmp session dir and unset env vars.

    No ``TG_CODE`` / ``TG_2FA_PASSWORD`` are set — we always pass the
    prompts explicitly so the env-var fallback path is bypassed (it
    has its own cap and disconnected-mode behaviour that's not under
    test here).
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


def _make_real_session_client(*, authorized_after_signin: bool = True):
    """Build a TelegramClient stand-in that uses a REAL SQLiteSession.

    This is the closest thing to a faithful end-to-end test we can
    run without making real network calls: the mock simulates what
    Telethon does internally (``_try_gen_auth_key`` →
    ``_auth_key_callback`` → ``session.auth_key = key`` →
    ``session.save()``) using the actual
    :class:`telethon.sessions.SQLiteSession`, so the on-disk file
    is real and we can inspect it with ``sqlite3``.

    Returns ``(client_factory, session_path_factory)`` — call
    ``session_path_factory()`` to get the path the next constructed
    client will use (the factory stashes it on a class attribute
    that ``__init__`` reads).
    """
    from telethon.sessions import SQLiteSession

    class _State:
        session_path: str = ""

    state = _State()

    class RealSessionClient:
        def __init__(self, session_path, *args, **kwargs):
            # Telethon passes the path WITHOUT the .session suffix.
            self.session = SQLiteSession(str(session_path))
            # Real Telethon loads the auth_key from the SQLite DB
            # at client construction time. Mirror that here so a
            # second client opening the same file is already
            # "authorized" without needing to re-run sign_in.
            self._sender = MagicMock()
            self._sender.auth_key = self.session._auth_key
            self._authorized = self.session._auth_key is not None

        async def connect(self):
            # If we already have an auth_key (loaded from the DB
            # at construction), there's nothing to generate. This
            # mirrors what real Telethon does: _try_gen_auth_key
            # only fires when ``self.auth_key`` is empty.
            if self._sender.auth_key is None:
                new_key = AuthKey(data=b"\x02" * 256)
                self._sender.auth_key = new_key
                self.session.auth_key = new_key
                self.session.save()
            return True

        async def is_user_authorized(self):
            return self._authorized

        async def send_code_request(self, phone):
            return MagicMock(phone_code_hash="hash")

        async def sign_in(self, *args, code=None, password=None, **kwargs):
            if password is not None:
                # 2FA path: pretend the cloud password is accepted.
                self._authorized = authorized_after_signin
                return None
            # OTP path: pretend 2FA is required.
            raise SessionPasswordNeededError(request=None)

        async def disconnect(self):
            try:
                self.session.close()
            except Exception:
                pass
            return True

        async def get_me(self):
            return MagicMock(
                spec=TgUser,
                id=999,
                first_name="Tester",
                username="tester",
                phone="+15555550100",
            )

    return RealSessionClient


# --- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_file_exists_after_successful_login(tmp_path, monkeypatch):
    """The headline regression: after a successful ``tglogin``, the
    on-disk .session file MUST exist and contain a non-empty
    ``auth_key`` blob — without this, the long-running service
    has nothing to load on its next start.

    Without the host-side ``cmd_login`` restart, the running app's
    in-memory Telethon client is still bound to its startup-time
    SQLite handle (which was opened when no session existed), so
    it keeps reporting ``is_connected=False`` even though the file
    on disk is now valid. This test pins the persistence invariant
    so a future refactor of the login flow can't quietly regress
    it.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path)
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    session_path = tmp_path / "session" / "test.session"
    assert not session_path.exists(), "precondition: no session file"

    codes = iter(["11111"])
    passwords = iter(["secret"])

    await svc.start(
        code_prompt=lambda: next(codes),
        password_prompt=lambda: next(passwords),
    )
    await svc.stop()

    assert session_path.exists(), (
        f"login reported success but {session_path} was not created"
    )

    # Inspect the SQLite DB directly — Telethon's schema has a
    # single row in `sessions` whose `auth_key` column must be a
    # non-empty blob (256 bytes for a real auth_key).
    conn = sqlite3.connect(str(session_path))
    try:
        cursor = conn.execute("select length(auth_key) from sessions")
        (auth_key_length,) = cursor.fetchone()
    finally:
        conn.close()

    assert auth_key_length == 256, (
        f"session file was created but auth_key is "
        f"{auth_key_length} bytes — expected 256. The persistence "
        f"path is broken."
    )


@pytest.mark.asyncio
async def test_session_reusable_across_fresh_telegramclient(tmp_path, monkeypatch):
    """Stronger guarantee: the .session file written by ``tglogin``
    MUST be usable by a brand-new ``TelegramClient`` constructed
    later — ``is_user_authorized()`` must return True without any
    interactive prompt.

    This is the property the long-running service depends on after
    ``docker compose restart``: its lifespan re-runs, builds a new
    Telethon client against the same session path, and expects
    ``is_user_authorized()`` to short-circuit because the file is
    already populated.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path)
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    await svc.start(
        code_prompt=lambda: "11111",
        password_prompt=lambda: "secret",
    )
    await svc.stop()

    # Now build a brand-new TelegramService that points at the same
    # session file. Its start() must NOT prompt for a code — the
    # on-disk auth_key is enough to establish an authorized
    # session. We simulate this by checking that start() returns
    # without calling any prompt callback.
    fresh_svc = _build_service(tmp_path)
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    prompts_called = []

    def _must_not_be_called():
        prompts_called.append("called")
        return "11111"

    await fresh_svc.start(
        code_prompt=_must_not_be_called,
        password_prompt=_must_not_be_called,
    )

    assert prompts_called == [], (
        f"start() invoked a prompt callback even though the "
        f"session was already authorized: {prompts_called}"
    )
    assert fresh_svc.is_connected is True


@pytest.mark.asyncio
async def test_session_file_written_even_when_login_aborted_before_2fa(
    tmp_path, monkeypatch
):
    """Defensive: a 2FA-enabled account must still leave a valid
    ``auth_key`` on disk even if the operator aborts the password
    prompt with Ctrl-C after the OTP was accepted.

    The OTP step calls ``sign_in(phone, code)`` → server raises
    ``SessionPasswordNeededError`` → we prompt for the password →
    operator aborts. By this point, ``connect()`` has already run,
    so ``_auth_key_callback`` has already persisted the auth_key.
    The session file should be present (even if not yet authorized
    by the user), because a future re-login just needs to prompt
    for a fresh code — Telethon will reuse the same auth_key.

    This catches a regression where a fix would accidentally couple
    the auth_key persistence to the success of the OTP step.
    """
    monkeypatch.delenv("TG_CODE", raising=False)
    monkeypatch.delenv("TG_2FA_PASSWORD", raising=False)
    svc = _build_service(tmp_path)
    RealClient = _make_real_session_client()
    monkeypatch.setattr("app.telegram_client.TelegramClient", RealClient)

    session_path = tmp_path / "session" / "test.session"

    # The OTP succeeds, then the operator hits Ctrl-C during the
    # 2FA prompt. We simulate this by raising KeyboardInterrupt
    # from the password_prompt callback (which is a sync callable
    # in our code, not async).
    def _abort_at_2fa():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        await svc.start(
            code_prompt=lambda: "11111",
            password_prompt=_abort_at_2fa,
        )

    # The auth_key was persisted during connect() (before the OTP
    # step), so the file should still be there.
    assert session_path.exists(), (
        "session file should be on disk after connect() even if "
        "the operator aborts before completing 2FA"
    )
