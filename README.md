# Telegram Automation API

> **Looking for the production deployment guide?** See
> [`DEPLOYMENT.md`](DEPLOYMENT.md) — covers the Docker Compose setup, the
> `dnnotification` CLI, host layout, and security model.

Production-ready FastAPI service that connects to a **personal Telegram account**
(via MTProto / Telethon — *not* the Bot API) and exposes two REST endpoints for
n8n (or any HTTP client) to use:

* `POST /search` — search private (1-to-1) dialogs for a keyword
* `POST /send-voice` — send a predefined voice note to a user

Group chats and channels are ignored end-to-end. `FloodWaitError` is awaited
automatically. The session is persistent — no re-login on every request.

---

## Production install (one line)

Deploy on any Linux host with Docker. The installer downloads the
`dnnotification` CLI from the canonical repo, runs it with `sudo`, and then
asks for your Telegram credentials. The image
**`digitalneetwork/dn-notification:latest`** is pulled and started automatically:

```bash  
bash -c "$(curl -L https://raw.githubusercontent.com/Digitalweb-ir/dn-notification/main/dnnotification.sh)" @ install
```

After it finishes, `dnnotification` is on `PATH` and the stack is running.
Re-run with no arguments for the interactive menu:

```bash
dnnotification
```

The installer:

1. Verifies it is running as root.
2. Installs Docker via the official `get.docker.com` script (if missing).
3. Confirms the reinstall if `/opt/dn-notification` already exists.
4. Downloads `docker-compose.yaml` and `.env.example` from
   `Digitalweb-ir/dn-notification` (branch `main`).
5. Prompts for `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, and `API_KEY` and writes
   `/opt/dn-notification/.env` (mode `600`).
6. Copies itself to `/usr/local/bin/dnnotification` (extension stripped, +x).
7. Pulls the pre-built `digitalneetwork/dn-notification:latest` image and
   starts the container.

> For the full host layout, security checklist, and day-2 operations, see
> [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## Upgrading an existing install

```bash
sudo dnnotification update
```

The `update` flow:

1. Reads the currently installed version from inside the running container
   (`docker exec dn-notification cat /app/VERSION`).
2. Fetches the latest `VERSION` from `Digitalweb-ir/dn-notification@main`.
3. If the versions match, prints `You already have the latest version: …` and
   exits. Otherwise it prompts `Do you want to install it? (yes/no)`.
4. Downloads the latest `docker-compose.yaml` and `.env.example` into
   `/opt/dn-notification`.
5. **Merges** the existing `.env` with the new `.env.example`: every existing
   `KEY=VALUE` is preserved, and any newly introduced key in the template is
   prompted for interactively and appended. No user setting is ever overwritten.
6. Stops the stack (`docker compose down`).
7. Removes the existing `digitalneetwork/dn-notification:latest` image so the
   next pull is forced to fetch a fresh copy.
8. `docker compose pull && docker compose up -d`.

---

## Architecture

```
app/
├── __init__.py           # __version__ (kept in sync with ../VERSION)
├── main.py               # FastAPI app, lifespan, routes, auth
├── config.py             # Pydantic settings (loads .env, derives sub-paths from DATA_DIR)
├── logger.py             # Structured logging setup
├── models.py             # Pydantic request/response schemas
├── telegram_client.py    # Telethon wrapper (FloodWait-safe)
├── search_service.py     # Private-dialog cache + scoring search
└── voice_service.py      # Voice file mapping + send_file

dnnotification.sh         # Deployment CLI (installed to /usr/local/bin/dnnotification)
version_bump.sh           # Bumps VERSION based on commit-message keywords
VERSION                   # Single source of truth for the release version
docker-compose.yaml       # Single-service compose file (uses pre-built image)
Dockerfile                # Production image (Python 3.11 slim, non-root, tini, healthcheck)
.env.example              # Documented env template (copy to .env)
.github/workflows/        # CI: version_bump on push to main
```

### Simplified configuration

`/opt/dn-notification/.env` keeps only the values you actually configure. The
**only** path the user sets is `DATA_DIR`; voices, session, and logs
sub-directories are derived from it programmatically by `app/config.py`:

```python
@property
def voices_dir(self)  -> str: return f"{self.data_dir}/voices"
@property
def session_dir(self) -> str: return f"{self.data_dir}/session"
@property
def logs_dir(self)    -> str: return f"{self.data_dir}/logs"
```

The host bind-mount in `docker-compose.yaml` is the same single path
(`/var/lib/dn-notification` -> `/var/lib/dn-notification`), so the values
inside the container and on the host stay aligned without any extra wiring.

### Versioning

The release is pinned by a **git tag** (`vX.Y.Z`) — the tag is the canonical
release marker. `VERSION` and `app/__init__.py` are *derived* from the tag
plus the commits since it, kept in lockstep by `version_bump.sh`. The
running container reports its real release because the same `VERSION` file
is baked into the Docker image (copied to `/app/VERSION`).

`version_bump.sh` reads commit messages since the last version tag and bumps
accordingly:

| Commit prefix | Bump  | Example result    |
| ------------- | ----- | ----------------- |
| `break: …`    | major | `1.2.3 -> 2.0.0`  |
| `feat: …`     | minor | `1.2.3 -> 1.3.0`  |
| `fix: …`      | patch | `1.2.3 -> 1.2.4`  |
| (other)       | —     | version unchanged |

**The bump is applied locally, in the same commit as the change that
caused it.** A `commit-msg` hook at `.githooks/commit-msg` updates VERSION
and `__init__.py` automatically when the commit subject starts with one of
the recognised prefixes. The bump and the change land in one commit, so
local and remote never diverge.

To enable the hooks in a fresh clone:

```bash
make install-hooks
```

That installs the `commit-msg` and `pre-push` hooks from `.githooks/`
into your local `.git/hooks/` and verifies VERSION is in sync. Verify
with `make check-hooks`. The previous `git config core.hooksPath
.githooks` form still works if you prefer it, but the Makefile is the
documented path.

To skip the auto-bump for a one-off commit (e.g. `chore:`, `docs:`), set
`SKIP_VERSION_BUMP=1` in the environment, or use a prefix that isn't in
the table above.

CI (`.github/workflows/bump-version.yml`) does two things:

1. On every push to `main`, runs `./version_bump.sh --check`. If VERSION
   doesn't match the implied bump from the commits, the build fails. This
   catches the case where someone bypassed the hook (e.g. by amending a
   commit message).
2. On `v*.*.*` tag pushes, builds and publishes the Docker image with the
   tag as the image tag. Tags are created locally with
   `git tag vX.Y.Z && git push --tags` — they never appear from a CI
   auto-commit.


---

## Setup (local dev)

### 1. Get Telegram API credentials

Visit https://my.telegram.org/apps and create an application. You'll get an
`api_id` and `api_hash`.

### 2. Install dependencies

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and set TG_API_ID, TG_API_HASH, TG_PHONE, API_KEY
```

### 4. First-time login

The first run is interactive — Telethon needs the SMS code Telegram sends
to your phone (and your 2FA password, if enabled).

```bash
export TG_PHONE="+1234567890"
export TG_CODE="12345"          # code Telegram sent to your Telegram app
# If you have 2FA:
# export TG_2FA_PASSWORD="your_cloud_password"

python -c "import asyncio
from app.telegram_client import get_telegram_service
async def main():
    svc = get_telegram_service()
    await svc.start()
    await svc.stop()
asyncio.run(main())"
```

This creates a `telegram_session.session` file in the project root. **Keep this
file safe — anyone with it can access your Telegram account.**

### 5. Add voice files

Drop `.ogg` voice notes into `app/voices/`:

```
app/voices/expired.ogg
app/voices/welcome.ogg
app/voices/support.ogg
```

The notes must be valid OGG Opus (Telegram voice-note format). The Telegram
Desktop "Save as voice message" export is one easy way to produce these.

### 6. Run the API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Or, in production:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> ⚠️ Use a **single worker** — Telethon sessions are not safe to share across
> processes. Scale horizontally by running multiple instances each with its
> own session/account, not by adding workers.

---

## Docker

```bash
docker build -t telegram-automation .
docker run -d \
  --name telegram-automation \
  --env-file .env \
  -p 8000:8000 \
  -v $(pwd)/app/voices:/app/voices \
  -v $(pwd)/telegram_session.session:/app/telegram_session.session \
  telegram-automation
```

The voice and session files are mounted as volumes so they survive container
restarts.

---

## API

All endpoints (except `/health`) require the `X-API-KEY` header.

### `GET /health`

```bash
curl http://localhost:8000/health
```

```json
{"status": "ok", "telegram_connected": true, "session": "telegram_session"}
```

### `POST /search`

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: your_secret_key" \
  -d '{"query": "refund"}'
```

```json
{
  "query": "refund",
  "count": 2,
  "results": [
    {
      "chat_id": 123456789,
      "username": "alice",
      "name": "Alice Doe",
      "message": "I would like a refund please",
      "message_date": "2026-06-10T14:23:11+00:00",
      "message_id": 9876,
      "match_score": 0.9
    }
  ]
}
```

* Only **private** dialogs are scanned — groups and channels are skipped.
* Top **1–3** matches per chat (configurable via `SEARCH_TOP_MATCHES`).
* Results are sorted by most recent match date.

### `POST /send-voice`

```bash
curl -X POST http://localhost:8000/send-voice \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: your_secret_key" \
  -d '{"chat_id": 123456789, "template": "welcome"}'
```

```json
{
  "chat_id": 123456789,
  "template": "welcome",
  "file": "welcome.ogg",
  "message_id": 42,
  "sent_at": "2026-06-11T09:00:00+00:00"
}
```

Templates map to files in `app/voices/`:

| template | file               |
| -------- | ------------------ |
| expired  | expired.ogg        |
| welcome  | welcome.ogg        |
| support  | support.ogg        |
| custom   | (uses welcome.ogg) |

> The service refuses to send to bots or non-user entities as a safety
> check. Telegram `FloodWaitError`s are awaited and retried automatically.

---

## Configuration

The only path you set is `DATA_DIR`; everything else is derived from it.

| Env var                 | Default                    | Description                                                                     |
| ----------------------- | -------------------------- | ------------------------------------------------------------------------------- |
| `TG_API_ID`             | —                          | From my.telegram.org                                                            |
| `TG_API_HASH`           | —                          | From my.telegram.org                                                            |
| `TG_PHONE`              | —                          | Phone number in international format                                            |
| `TG_SESSION_NAME`       | `telegram_session`         | Name of the persistent .session file                                            |
| `API_KEY`               | —                          | Required `X-API-KEY` header value                                               |
| `HOST` / `PORT`         | `0.0.0.0` / `8000`         | Bind address                                                                    |
| `HOST_PORT`             | `8000`                     | Host port mapping in docker-compose                                             |
| `LOG_LEVEL`             | `INFO`                     | Log level                                                                       |
| `SEARCH_LIMIT_PER_CHAT` | `200`                      | Max messages fetched per dialog at warmup                                       |
| `SEARCH_TOP_MATCHES`    | `3`                        | Max matches returned per chat                                                   |
| `SEARCH_CACHE_TTL`      | `300`                      | Seconds before dialog cache is refreshed                                        |
| `DATA_DIR`              | `/var/lib/dn-notification` | Persistent data root (in-container) — voices/session/logs are derived from this |

The deployment image and the GitHub repo are **not** configurable:

* Image: `digitalneetwork/dn-notification:latest`
* Repo:  `https://github.com/Digitalweb-ir/dn-notification` (branch `main`)

---

## n8n usage

In n8n, use the **HTTP Request** node:

1. **Authentication**: Generic Credential Type → Header Auth with
   `X-API-KEY: your_secret_key`
2. **Method**: POST
3. **URL**: `http://your-server:8000/search` (or `/send-voice`)
4. **Body**: JSON

You can chain: search → loop over `results` → for each match, call
`/send-voice` with the `chat_id` and a chosen `template`.

---

## Security notes

* `telegram_session.session` is a credential — treat it like a password. Do
  **not** commit it. It is in `.gitignore` and `.dockerignore`.
* The session grants full access to your Telegram account. Run this service
  on a host you trust.
* Always set a strong `API_KEY` — the API has no per-user model; anyone with
  the key can send voice notes from your account.
* Consider running the service behind a reverse proxy (nginx, Caddy) and
  putting it on a private network.
