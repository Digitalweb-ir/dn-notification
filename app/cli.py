"""cli — operator CLI for the dn-notification service.

This module is the in-container entry point for the day-2 operations
that the FastAPI app does not handle on its own — most importantly,
the **first-time Telegram login**, which is interactive (it has to
ask the user for the SMS code Telegram sent them, and that code is
single-use so it cannot be stored).

Usage (inside the container)::

    python -m app.cli tglogin
    python -m app.cli status

From the host, ``dnnotification login`` is a thin wrapper that runs
this CLI via ``docker exec`` and (for ``tglogin``) drives the
interactive login in the same process as the running service via a
``POST /admin/handoff`` so the running service picks up the new
session without a container restart.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from datetime import datetime, timezone

import httpx
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

# Address of the running FastAPI process inside the container. The
# CLI and the server share the same container, so loopback works
# without going through the host's port mapping. The port mirrors
# docker-compose.yaml's service port.
_SERVICE_BASE_URL = "http://127.0.0.1:8000"


def _build_service() -> TelegramService:
    """Return a fresh TelegramService wired to the current env/.env.

    We intentionally bypass :func:`get_telegram_service` (the cached
    singleton the FastAPI app uses) — the CLI is a short-lived
    process, and reusing the singleton across CLI invocations would
    pin a stale connection in memory.
    """
    return TelegramService(get_settings())


async def _handoff_to_running_service(api_key: str) -> tuple[bool, str]:
    """Tell the running FastAPI service to reload from the on-disk session.

    The CLI's sign-in writes a fresh auth_key to the bind-mounted
    .session file. The running service has its own in-memory client
    built at startup that is now stale. POSTing to ``/admin/handoff``
    tells it to stop the stale client and start a new one that
    loads the authorized auth_key from disk.

    Returns ``(success, message)`` — on success the running service
    is live and ``/search``/``/send-voice`` will return real results;
    on failure the on-disk session is still valid and ``dnnotification
    restart`` will pick it up.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{_SERVICE_BASE_URL}/admin/handoff",
                headers={"X-API-KEY": api_key},
            )
    except httpx.HTTPError as exc:
        return False, (
            f"Could not reach the running service at {_SERVICE_BASE_URL} "
            f"({exc.__class__.__name__}: {exc}). The on-disk session is "
            f"still valid — run `dnnotification restart` to load it."
        )

    if response.status_code == 200:
        body = response.json()
        if body.get("already_connected"):
            return True, "Service was already connected."
        return True, "Service is now connected."

    # Surface the server's error verbatim so the operator can act on it.
    try:
        body = response.json()
        error = body.get("error") or body.get("detail") or response.text
    except Exception:
        error = response.text
    return False, (
        f"Handoff returned HTTP {response.status_code}: {error}. "
        f"The on-disk session is still valid — run `dnnotification restart` "
        f"to load it."
    )


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
    """Sign in to Telegram interactively and hand off to the running service.

    On success the .session file is written to the host bind-mount
    (``$DATA_DIR/session/<name>.session``) and the running FastAPI
    service is told (via ``POST /admin/handoff``) to reload its
    in-memory client against the new auth_key. Endpoints like
    ``/search`` and ``/send-voice`` become live immediately — no
    container restart needed.

    The CLI owns its own short-lived ``TelegramClient`` for the
    duration of the sign-in. The "Disconnecting Telegram client"
    line at the end is the CLI's client being torn down after the
    handoff; it is expected and does not mean the session is lost.
    """
    setup_logging()
    service = _build_service()
    api_key = get_settings().api_key

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
            me = await service.require_client().get_me()
            if isinstance(me, User):
                display = getattr(me, "username", None) or me.first_name or "?"
                typer.echo(
                    f"Login successful — signed in as {display} "
                    f"(id={me.id}, phone={me.phone}).",
                    err=True,
                )
            else:
                typer.echo("Login successful.", err=True)

            # Hand off the new auth_key to the running service
            # BEFORE we disconnect our own client. This is the step
            # that makes the new session "stick" — the running
            # service's stale client (built at startup when no
            # session existed) is stopped, a fresh one is started
            # that loads the auth_key from disk, and ``is_connected``
            # flips to True. Endpoints stop returning 503.
            ok, message = await _handoff_to_running_service(api_key)
            if ok:
                typer.echo(message, err=True)
                typer.echo(
                    "Session is live — /search and /send-voice are ready.",
                    err=True,
                )
            else:
                typer.echo(f"Warning: {message}", err=True)
        except KeyboardInterrupt:
            typer.echo("Aborted by user.", err=True)
            return 130
        except RuntimeError as exc:
            typer.echo(f"Login failed: {exc}", err=True)
            return 1
        finally:
            # Always stop the CLI's client. This produces the
            # "Disconnecting Telegram client" log line — it is the
            # CLI's client being torn down, NOT the running
            # service's. The running service's client is owned by
            # the FastAPI process and lives until container stop.
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
        typer.echo("Status: NO SESSION — run `dnnotification login` first.", err=True)
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
                "(run `dnnotification login` to sign in).",
                err=True,
            )
            return 1

        typer.echo("Status: AUTHORIZED")
        return 0

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    app()
