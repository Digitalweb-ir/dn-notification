# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**DN Notification** — A FastAPI service that connects to a personal Telegram account (via MTProto / Telethon) and exposes REST endpoints for n8n workflow automation. Used for support workflows: searching private dialogs and sending messages.

- Docker image: `digitalneetwork/dn-notification`
- Production data root: `/var/lib/dn-notification`
- Deployment CLI: `dnnotification.sh` (installed as `/usr/local/bin/dnnotification`)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (dev)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

# Run tests
pytest tests/ -v

# Run a single test
pytest tests/test_tglogin_2fa.py -v

# Python CLI (inside container or locally)
python -m app.cli tglogin
python -m app.cli status

# Docker compose lifecycle (via dnnotification helper)
dnnotification up
dnnotification down
dnnotification logs
dnnotification cli tglogin
dnnotification update
```

## Architecture

### Single-worker FastAPI with one TelegramClient instance

- **Uvicorn workers must be 1** — `TelegramService` is a singleton managing one Telethon `TelegramClient`. Scale horizontally (multiple Docker containers), not with workers.
- **Disconnected-mode startup** — The app starts without crashing if no Telegram session exists. Endpoints return 401 with a hint to run `tglogin`. No Telethon client is constructed during lifespan on a fresh install.
- **Auto-reconnect via `ensure_connected()`** — When a session file appears on disk (after CLI `tglogin`), the next API request triggers `ensure_connected()` which detects the new session, constructs a TelegramClient, and reconnects automatically.

### Service layers

| Layer | File | Role |
|-------|------|------|
| `TelegramService` | `app/telegram_client.py` | Telethon client wrapper with FloodWait handling, connection state management, auto-reconnect via `ensure_connected()` |
| `SearchService` | `app/search_service.py` | Private-dialog cache (TTL-based), global server-side search (exact match, private dialogs only) |
| `MessageService` | `app/message_service.py` | Sends messages — Telegram Business Quick Reply or direct text |
| `Settings` | `app/config.py` | Pydantic Settings from env; `DATA_DIR` is the single storage root — `session_dir`, `logs_dir` derived from it |

### Login flow

`app/cli.py` directly creates a Telethon `TelegramClient` in the CLI process, drives the OTP/2FA login flow, saves the session to the shared session path, then disconnects. The FastAPI process auto-detects the new session file on the next API request via `ensure_connected()` — no admin endpoints, no HTTP coordination, no asyncio Events. The session file on disk is the only handoff mechanism between the CLI and the FastAPI app.

### API endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | None | Ping + connection status |
| POST | `/search` | API Key + Session | Search private dialogs |
| POST | `/send-message` | API Key + Session | Send message (Quick Reply or direct text) |

### Version resolution (`app/__init__.py`)

1. `APP_VERSION` env var (production — set from git tag in Docker CI)
2. `git describe --tags --dirty` (development)
3. `"0.0.0+unknown"` (fallback)

## Versioning & CI

- **Conventional Commits** — `fix:` → patch, `feat:` → minor, `BREAKING CHANGE:` → major
- **semantic-release** in CI creates GitHub tags and releases on push to `main`
- **CI** (`.github/workflows/release.yml`): test → semantic-release → multi-arch Docker build+push
- Docker images tagged with both semver and `latest`
- CI secrets required: `GH_TOKEN` (contents:write), `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`

## Key Patterns

- **DATA_DIR** is the only path that needs to be configured; everything else is derived from it
- **No VERSION file** — git tag is the single source of truth, propagated via CI job outputs
- **Login is CLI-only** — `tglogin` creates a TelegramClient directly in the CLI process; no admin endpoints
- **Session file as handoff** — the CLI writes the session file; the FastAPI app reads it via `ensure_connected()` on the next API request
- **Endpoints are guarded** by two dependency layers: API key auth (`X-API-KEY` header) and async Telegram session check (auto-reconnects if session file exists)
