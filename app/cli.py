"""cli — operator CLI for the dn-notification service.

This module provides two commands:

* ``tglogin`` — Creates a TelegramClient directly, drives the OTP/2FA
  login flow against the real Telegram API, and saves the session file
  to the same path the FastAPI app reads.  After this command exits
  successfully, the next API request to the FastAPI app auto-detects the
  new session file and reconnects — no admin endpoints, no restart, no
  handoff.

* ``status`` — Checks whether a Telegram session exists on disk and
  whether the running FastAPI service is connected, then prints a
  human-readable summary.

Usage (inside the container, typically via the host's
``dnnotification cli tglogin`` wrapper which does the docker exec
with a TTY allocated)::

    python -m app.cli tglogin
    python -m app.cli status
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from datetime import datetime, timezone
from typing import Optional

import httpx
import typer

from .config import get_settings
from .logger import setup_logging

app = typer.Typer(
    name="dnapp-cli",
    help="Operator CLI for dn-notification (Telegram login, status, …).",
    no_args_is_help=True,
    add_completion=False,
)


def _prompt_code() -> Optional[str]:
    """Prompt the user for the SMS code. Empty input signals abort.

    The SMS code is digits-only, so trimming surrounding whitespace is
    safe and protects against stray newlines from copy-paste.
    """
    try:
        value = getpass.getpass("Telegram login code (empty to abort): ")
    except (EOFError, KeyboardInterrupt):
        print("")  # newline after the ^C / ^D
        raise
    return value.strip() or None


def _prompt_password() -> Optional[str]:
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
    """Sign in to Telegram by creating a temporary client.

    Creates a TelegramClient in this CLI process, performs the OTP / 2FA
    flow against the real Telegram API, saves the session to the same
    ``session_path`` the FastAPI app reads, then disconnects. The next API
    request to the FastAPI app auto-detects the new session file and
    reconnects — no admin endpoints, no restart.
    """
    setup_logging()
    settings = get_settings()
    session_path = settings.session_path
    session_dir = session_path.parent

    if not session_dir.is_dir():
        typer.echo(f"Session directory {session_dir} is missing.", err=True)
        raise SystemExit(1)

    async def _run() -> int:
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

        client = TelegramClient(
            str(session_path),
            settings.tg_api_id,
            settings.tg_api_hash,
            device_model="TelegramAutomationCLI",
            system_version="1.0",
            app_version="1.0.0",
            lang_code="en",
            system_lang_code="en",
        )

        try:
            await client.connect()
        except Exception as exc:
            typer.echo(f"Could not connect to Telegram: {exc}", err=True)
            return 1

        # Already authorized?
        if await client.is_user_authorized():
            me = await client.get_me()
            display = getattr(me, "username", None) or getattr(me, "first_name", "?")
            typer.echo(
                f"Already authorized as {display} (id={me.id}, phone={me.phone}). "
                f"The next API request will auto-reconnect.",
                err=True,
            )
            await client.disconnect()
            return 0

        phone = settings.tg_phone
        typer.echo(f"Connecting to Telegram as {phone}…", err=True)

        # Send code request
        try:
            await client.send_code_request(phone)
        except ApiIdInvalidError as exc:
            typer.echo(
                f"Telegram rejected TG_API_ID / TG_API_HASH ({exc}). "
                f"Check the values from https://my.telegram.org/apps.",
                err=True,
            )
            await client.disconnect()
            return 1
        except FloodWaitError as exc:
            typer.echo(
                f"Flood wait: must wait {exc.seconds}s before retrying.",
                err=True,
            )
            await client.disconnect()
            return 1

        typer.echo(
            "Telegram sent an SMS code to your account. Enter it below.",
            err=True,
        )

        # OTP loop
        while True:
            try:
                code = _prompt_code()
            except (KeyboardInterrupt, EOFError):
                typer.echo("Aborted by user.", err=True)
                await client.disconnect()
                return 130

            if code is None:
                typer.echo("Aborted (empty code).", err=True)
                await client.disconnect()
                return 1

            try:
                await client.sign_in(phone, code)
                break  # Success — no 2FA needed
            except SessionPasswordNeededError:
                break  # Code accepted, need 2FA
            except PhoneCodeInvalidError:
                typer.echo("Invalid code. Try again.", err=True)
                continue
            except PhoneCodeExpiredError:
                typer.echo("Code expired. Requesting a new one…", err=True)
                await client.send_code_request(phone)
                continue
            except ApiIdInvalidError as exc:
                typer.echo(f"API credentials rejected: {exc}", err=True)
                await client.disconnect()
                return 1

        # 2FA password loop (only reached if SessionPasswordNeededError)
        if not await client.is_user_authorized():
            typer.echo(
                "Account has 2FA enabled. Enter cloud password.",
                err=True,
            )
            while True:
                try:
                    password = _prompt_password()
                except (KeyboardInterrupt, EOFError):
                    typer.echo("Aborted by user.", err=True)
                    await client.disconnect()
                    return 130

                if not password:
                    typer.echo(
                        "Empty password. Account has 2FA — you must enter it.",
                        err=True,
                    )
                    await client.disconnect()
                    return 1

                try:
                    await client.sign_in(password=password)
                    break
                except (PasswordHashInvalidError, SrpIdInvalidError) as exc:
                    typer.echo(
                        f"Password rejected ({type(exc).__name__}). "
                        f"Try again — the cloud password is case-sensitive "
                        f"and whitespace matters; do not trim it.",
                        err=True,
                    )
                    continue

        # Verify
        me = await client.get_me()
        display = getattr(me, "username", None) or getattr(me, "first_name", "?")
        typer.echo(
            f"Login successful — signed in as {display} "
            f"(id={me.id}, phone={me.phone}).",
            err=True,
        )

        # Disconnect — session is now saved to disk.
        await client.disconnect()
        typer.echo(
            "Session saved. The FastAPI service will auto-reconnect "
            "on the next request.",
            err=True,
        )
        return 0

    sys.exit(asyncio.run(_run()))


@app.command()
def status() -> None:
    """Report whether a Telegram session exists and is usable.

    Checks the session file on disk and queries the running service's
    ``/health`` endpoint. Exits 0 if the service is connected; exits 1
    otherwise.
    """
    setup_logging()
    settings = get_settings()
    session_path = settings.session_path

    # Check session file locally
    typer.echo(f"Session file: {session_path}")
    if session_path.exists():
        mtime = datetime.fromtimestamp(
            session_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        typer.echo(f"Last modified: {mtime}")
    else:
        typer.echo("No session file on disk.")

    # Check the running service
    async def _run() -> int:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get("http://127.0.0.1:8000/health")
            body = resp.json()
            if body.get("telegram_connected"):
                typer.echo("Status: AUTHORIZED — the service is connected.")
                return 0
            else:
                typer.echo(
                    "Status: NOT CONNECTED — the service is not using a "
                    "Telegram session. Run `dnnotification cli tglogin` "
                    "to sign in.",
                    err=True,
                )
                return 1
        except httpx.HTTPError as exc:
            typer.echo(
                f"Could not reach the service ({exc.__class__.__name__}). "
                "Is the container running? "
                "Try `dnnotification up` or `docker logs dn-notification`.",
                err=True,
            )
            return 1

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    app()
