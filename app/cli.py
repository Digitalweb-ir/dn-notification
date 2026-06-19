"""cli — operator CLI for the dn-notification service.

This module is the in-container entry point for the day-2 operations
that the FastAPI app does not handle on its own — most importantly,
the **first-time Telegram login**, which is interactive (it has to
ask the user for the SMS code Telegram sent them, and that code is
single-use so it cannot be stored).

Usage (inside the container)::

    python -m app.cli tglogin
    python -m app.cli status

From the host, ``dnnotification cli tglogin`` is a thin wrapper that
runs this CLI via ``docker exec`` and (for ``tglogin``) restarts the
service so the newly-written ``.session`` file is picked up.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from datetime import datetime, timezone

import typer
from telethon.tl.types import User

from .config import get_settings
from .logger import setup_logging
from .telegram_client import TelegramService

app = typer.Typer(
    name="dnapp-cli",
    help="Operator CLI for dn-notification (Telegram login, status, …).",
    no_args_is_help=True,
    add_completion=False,
)


def _build_service() -> TelegramService:
    """Return a fresh TelegramService wired to the current env/.env.

    We intentionally bypass :func:`get_telegram_service` (the cached
    singleton the FastAPI app uses) — the CLI is a short-lived
    process, and reusing the singleton across CLI invocations would
    pin a stale connection in memory.
    """
    return TelegramService(get_settings())


def _prompt_code() -> str | None:
    """Prompt the user for the SMS code. Empty input aborts the prompt loop.

    The SMS code is digits-only, so trimming surrounding whitespace is
    safe and protects against stray newlines from copy-paste.
    """
    try:
        value = getpass.getpass("Telegram login code (empty to abort): ")
    except (EOFError, KeyboardInterrupt):
        print("")  # newline after the ^C / ^D
        raise
    return value.strip() or None


def _prompt_password() -> str | None:
    """Prompt the user for the 2FA cloud password.

    CRITICAL: do NOT strip the value. Telegram 2FA cloud passwords
    are user-defined and may legitimately contain leading/trailing
    whitespace, which is hashed into the stored password. Stripping
    the input produces a wrong hash and Telethon rejects it with
    ``PasswordHashInvalidError`` even when the user is sure the
    password is correct. The value is returned verbatim (an empty
    string is mapped to None so the caller can treat it as "abort").
    """
    try:
        value = getpass.getpass("2FA cloud password (empty to abort): ")
    except (EOFError, KeyboardInterrupt):
        print("")
        raise
    return value if value else None


@app.command()
def tglogin() -> None:
    """Sign in to Telegram interactively and write a persistent .session file.

    On success the .session file is written to the host bind-mount
    (``$DATA_DIR/session/<name>.session``). The container should be
    restarted afterwards — the running service has its own in-memory
    Telethon client which was built before the session existed. The
    host-side ``dnnotification cli tglogin`` command does the restart
    for you.
    """
    setup_logging()
    service = _build_service()

    async def _run() -> int:
        typer.echo(
            f"Connecting to Telegram as {service.settings.tg_phone}…",
            err=True,
        )
        try:
            await service.start(
                code_prompt=_prompt_code,
                password_prompt=_prompt_password,
            )
            # start() already verified get_me() internally and logged
            # the user; fetch it again here so the operator sees it on
            # stdout, then disconnect.
            me = await service.require_client().get_me()
            if isinstance(me, User):
                display = getattr(me, "username", None) or me.first_name or "?"
                typer.echo(
                    f"Login successful — signed in as {display} "
                    f"(id={me.id}, phone={me.phone})."
                )
            else:
                typer.echo("Login successful.")
        except KeyboardInterrupt:
            typer.echo("Aborted by user.", err=True)
            return 130
        except RuntimeError as exc:
            typer.echo(f"Login failed: {exc}", err=True)
            return 1
        finally:
            await service.stop()
        return 0

    sys.exit(asyncio.run(_run()))


@app.command()
def status() -> None:
    """Report whether a Telegram session exists and is usable.

    Exits 0 if a session file is present and the credentials inside it
    are accepted by Telegram; exits 1 otherwise. Intended for use from
    the host CLI (`dnnotification status`) and from cron/monitoring.
    """
    settings = get_settings()
    session_path = settings.session_path
    typer.echo(f"Session file: {session_path}")

    if not session_path.exists():
        typer.echo("Status: NO SESSION — run `dnnotification cli tglogin` first.", err=True)
        raise typer.Exit(code=1)

    mtime = datetime.fromtimestamp(session_path.stat().st_mtime, tz=timezone.utc)
    typer.echo(f"Last modified: {mtime.isoformat()}")

    service = _build_service()

    async def _run() -> int:
        # Calling start() with no prompts triggers the env-var path.
        # If the session is already authorized, start() returns
        # immediately without prompting (the is_user_authorized()
        # fast path inside start()) and sets ``is_connected=True``.
        #
        # If the session is missing or invalid, ``start()`` no longer
        # raises: it now falls back to "disconnected mode" so the
        # FastAPI lifespan does not crash-loop. We therefore check
        # ``is_connected`` ourselves to decide what to report.
        try:
            await service.start()
        except RuntimeError as exc:
            typer.echo(f"Status: SESSION INVALID — {exc}", err=True)
            return 1
        finally:
            await service.stop()

        if not service.is_connected:
            typer.echo(
                "Status: SESSION INVALID — no authorized session "
                "(run `dnnotification cli tglogin` to sign in).",
                err=True,
            )
            return 1

        typer.echo("Status: AUTHORIZED")
        return 0

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    app()
