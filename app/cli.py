"""cli — operator CLI for the dn-notification service.

This module is a **thin HTTP client** that drives admin endpoints on
the running FastAPI process. There is intentionally no
``TelegramService`` instantiated here: the FastAPI process owns the
single ``TelegramService`` singleton, and both the operator-facing
endpoints (``/search``, ``/send-voice``) and the operator-driven
admin login endpoints (``/admin/login/...``) call into it. The CLI
is the operator's way to type prompts (SMS code, 2FA password) and
forward them over HTTP. After ``tglogin`` completes, the same
singleton the endpoints use is now authorized, so the user does
not need to restart anything.

Usage (inside the container, typically via the host's
``dnnotification cli tglogin`` wrapper which does the docker exec
with a TTY allocated)::

    python -m app.cli tglogin
    python -m app.cli status

The Typer subcommand list is meant to grow: each new operator
action is a new subcommand here + a new admin endpoint in
``app/main.py``; the shared glue in this module is just the
``_request`` helper that wraps ``httpx``.
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from typing import Any, Mapping, Optional

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

# Address of the running FastAPI process inside the container. The
# CLI and the server share the same container, so loopback works
# without going through the host's port mapping. The port mirrors
# docker-compose.yaml's service port.
_SERVICE_BASE_URL = "http://127.0.0.1:8000"

# Long timeout: the operator may take a long time to fetch the SMS
# code, and 2FA passwords may be typed while looking at a password
# manager. The login can be slow on the wire too (Telegram's
# send_code_request can take several seconds on a cold connection).
_HTTP_TIMEOUT = 600.0


async def _request(
    method: str,
    path: str,
    *,
    api_key: str,
    json_body: Optional[Mapping[str, Any]] = None,
) -> httpx.Response:
    """Issue an HTTP call to the running FastAPI process.

    Centralised so every command authenticates the same way and
    surfaces a uniform error if the running service is unreachable
    (the CLI cannot proceed if the service is down — every command
    assumes a live FastAPI process).
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            return await client.request(
                method,
                f"{_SERVICE_BASE_URL}{path}",
                headers={"X-API-KEY": api_key},
                json=json_body,
            )
    except httpx.HTTPError as exc:
        # The service is down or unreachable. The host wrapper
        # (`dnnotification cli …`) auto-starts the container, but a
        # crash mid-flight lands here. Surface a clear hint and let
        # the operator act.
        raise SystemExit(
            f"Could not reach the running service at {_SERVICE_BASE_URL} "
            f"({exc.__class__.__name__}: {exc}). Is the container up? "
            f"Try `dnnotification up` or `docker logs dn-notification`."
        ) from exc


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
    """Sign in to Telegram by driving the running service's admin login.

    The operator types the SMS code and (if required) the 2FA
    password at the prompts; this command forwards them to the
    running FastAPI process, which performs the sign-in against
    its own ``TelegramService`` singleton. After this command
    exits successfully, ``/search`` and ``/send-voice`` are live —
    no restart, no handoff, no extra container.
    """
    setup_logging()
    api_key = get_settings().api_key

    async def _run() -> int:
        typer.echo(
            f"Connecting to Telegram as {get_settings().tg_phone}…",
            err=True,
        )

        # Step 1: kick off the login in the running service.
        try:
            response = await _request("POST", "/admin/login/start", api_key=api_key)
        except SystemExit as exc:
            typer.echo(str(exc), err=True)
            return 1

        if response.status_code == 409:
            typer.echo(
                "Another login is already in progress. Wait for it to "
                "finish or fail, then re-run this command.",
                err=True,
            )
            return 1

        if response.status_code != 200:
            typer.echo(
                f"Login start failed: HTTP {response.status_code} — {response.text}",
                err=True,
            )
            return 1

        body = response.json()
        if body.get("state") == "already_authorized":
            typer.echo(
                "Already authorized — no login needed. /search and "
                "/send-voice are live.",
                err=True,
            )
            return 0

        session_id: str = body["session_id"]
        typer.echo(
            "Telegram sent an SMS code to your account. Enter it below.",
            err=True,
        )

        # Step 2: drive the prompt loop. The running service's
        # TelegramService is awaiting the operator's code first, then
        # (if 2FA is enabled) the password. After each submit, the
        # response tells us the next state — we follow it until the
        # service reports ``complete`` or ``failed``.
        try:
            while True:
                # Read the current state to pick the right prompt.
                response = await _request(
                    "GET",
                    f"/admin/login/status?session_id={session_id}",
                    api_key=api_key,
                )
                if response.status_code != 200:
                    typer.echo(
                        f"Status poll failed: HTTP {response.status_code} — {response.text}",
                        err=True,
                    )
                    return 1
                state = response.json().get("state")
                if state in ("complete", "failed"):
                    break
                if state == "awaiting_code":
                    value = _prompt_code()
                elif state == "awaiting_password":
                    value = _prompt_password()
                else:
                    typer.echo(f"Unexpected login state: {state!r}", err=True)
                    return 1
                if value is None:
                    value = ""  # explicit abort
                # Submit the value; the response reflects the
                # post-submit state, which is what we'll read on
                # the next iteration.
                response = await _request(
                    "POST",
                    "/admin/login/submit",
                    api_key=api_key,
                    json_body={"session_id": session_id, "value": value},
                )
                if response.status_code != 200:
                    typer.echo(
                        f"Submit failed: HTTP {response.status_code} — {response.text}",
                        err=True,
                    )
                    return 1
        except KeyboardInterrupt:
            typer.echo("Aborted by user.", err=True)
            # Send an empty value so the running service's login
            # state machine exits with a clear "aborted" marker
            # instead of hanging on the prompt forever.
            await _request(
                "POST",
                "/admin/login/submit",
                api_key=api_key,
                json_body={"session_id": session_id, "value": ""},
            )
            return 130

        # Step 3: read the final state for the user-facing summary.
        response = await _request(
            "GET",
            f"/admin/login/status?session_id={session_id}",
            api_key=api_key,
        )
        if response.status_code != 200:
            typer.echo(
                f"Final status read failed: HTTP {response.status_code} — {response.text}",
                err=True,
            )
            return 1
        final = response.json()
        if final.get("state") == "complete":
            user = final.get("user") or {}
            display = user.get("username") or user.get("first_name") or "?"
            typer.echo(
                f"Login successful — signed in as {display} "
                f"(id={user.get('id')}, phone={user.get('phone')}).",
                err=True,
            )
            typer.echo(
                "Session is live — /search and /send-voice are ready.",
                err=True,
            )
            return 0
        typer.echo(
            f"Login failed: {final.get('error') or 'unknown error'}",
            err=True,
        )
        return 1

    sys.exit(asyncio.run(_run()))


@app.command()
def status() -> None:
    """Report whether a Telegram session exists and is usable.

    Hits ``GET /admin/status`` on the running service and prints
    a human-readable summary. Exits 0 if the service is
    authorized; exits 1 otherwise. Intended for use from the host
    CLI (`dnnotification cli status`) and from cron/monitoring.
    """
    setup_logging()
    api_key = get_settings().api_key

    async def _run() -> int:
        try:
            response = await _request("GET", "/admin/status", api_key=api_key)
        except SystemExit as exc:
            typer.echo(str(exc), err=True)
            return 1

        if response.status_code != 200:
            typer.echo(
                f"Status check failed: HTTP {response.status_code} — {response.text}",
                err=True,
            )
            return 1

        body = response.json()
        typer.echo(f"Session file: {body.get('session_file')}")
        mtime = body.get("session_mtime")
        if mtime:
            typer.echo(f"Last modified: {mtime}")
        if body.get("authenticated"):
            user = body.get("user") or {}
            display = user.get("username") or user.get("first_name") or "?"
            typer.echo(
                f"Status: AUTHORIZED — signed in as {display} "
                f"(id={user.get('id')}, phone={user.get('phone')})."
            )
            return 0
        typer.echo(
            "Status: NOT AUTHORIZED — run `dnnotification cli tglogin` to sign in.",
            err=True,
        )
        return 1

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    app()
