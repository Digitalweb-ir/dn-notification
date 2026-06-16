# Deployment Guide

Production deployment of the **DN Notification** Telegram MTProto automation
service using Docker Compose, managed by a single CLI: `dnnotification`.

---

## 1. Project layout

The CLI owns two top-level directories on the host. They are separated by
design so that project code and **sensitive session data** can be backed up,
restored, and permissioned independently. **All persistent application data —
including voice templates — lives under `/var/lib/dn-notification`**; the
container bind-mounts that single directory at the same path
(see `docker-compose.yaml`).

| Path                                | Purpose                                            | Default perms |
|-------------------------------------|----------------------------------------------------|---------------|
| `/opt/dn-notification`              | Project code, `docker-compose.yaml`, `.env`        | `755`         |
| `/var/lib/dn-notification`          | Persistent data — bind-mounted into the container  | `700`         |
| `/var/lib/dn-notification/session`  | Telegram `.session` file (account credential)      | `700`         |
| `/var/lib/dn-notification/logs`     | Application logs                                   | `755`         |
| `/var/lib/dn-notification/voices`   | Voice templates (`.ogg` files)                     | `755`         |

> The `.session` file grants **full access** to the Telegram account that
> signed in. Treat it like a password. The `700` permission on the data
> directory means only root (or a member of root's group) can read it.

> The other sub-paths (`session/`, `logs/`, `voices/`) are derived from
> `DATA_DIR` by `app/config.py` — they are **not** environment variables.

---

## 2. Canonical source-of-truth constants

These are hardcoded in the CLI and in `docker-compose.yaml`; do not parameterise
them with `.env` overrides or environment variables.

| What             | Value                                                                |
|------------------|----------------------------------------------------------------------|
| GitHub repo      | `https://github.com/Digitalweb-ir/dn-notification` (branch `main`)  |
| Docker image     | `digitalneetwork/dn-notification:latest`                             |
| Project dir      | `/opt/dn-notification`                                               |
| Data dir         | `/var/lib/dn-notification`                                           |

If you fork the project, edit the constants at the top of `dnnotification.sh`
and the `image:` line in `docker-compose.yaml`.

---

## 3. One-time host setup

### 3.1 Install Docker

The CLI auto-installs Docker (via the official `get.docker.com` script) the
first time you run `dnnotification install` if it isn't already present. If
you'd rather install it yourself:

```bash
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # then log out / back in

# CentOS / RHEL / Rocky
sudo yum install -y yum-utils
sudo yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo yum install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker

# macOS / Windows
# Install Docker Desktop: https://www.docker.com/products/docker-desktop
```

### 3.2 Install the CLI

Either run the one-line installer (recommended):

```bash
curl -fsSL https://raw.githubusercontent.com/Digitalweb-ir/dn-notification/main/dnnotification.sh \
    | sudo bash -s -- install
```

…or copy `dnnotification.sh` to your `PATH` manually:

```bash
sudo install -m 0755 dnnotification.sh /usr/local/bin/dnnotification
dnnotification version
```

---

## 4. First-time install

`dnnotification install`:

1. Verifies it is running as root.
2. Installs Docker via `get.docker.com` (if missing).
3. Confirms reinstall if `/opt/dn-notification` already exists.
4. Downloads `docker-compose.yaml` and `.env.example` from
   `Digitalweb-ir/dn-notification@main`.
5. Creates `/opt/dn-notification` and the data tree under
   `/var/lib/dn-notification/{session,logs,voices}`.
6. Sets `700` on the data directory, `chown 1000:1000` so the container's
   non-root user can write to the bind-mount.
7. Prompts for `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `API_KEY` and writes
   `.env` (mode `600`).
8. Copies itself to `/usr/local/bin/dnnotification` (extension stripped, +x).
9. Pulls `digitalneetwork/dn-notification:latest` and runs
   `docker compose up -d`.

For the local file paths that you must populate by hand before first use:

```bash
# Add at least one voice template.
sudo cp my-limited.ogg /var/lib/dn-notification/voices/limited.ogg
```

---

## 5. Configuration

`/opt/dn-notification/.env` is kept deliberately small. The **only** path you
configure is `DATA_DIR`; voices, session, and logs are derived from it in
`app/config.py`:

```python
@property
def voices_dir(self)  -> str: return f"{self.data_dir}/voices"
@property
def session_dir(self) -> str: return f"{self.data_dir}/session"
@property
def logs_dir(self)    -> str: return f"{self.data_dir}/logs"
```

The host bind-mount in `docker-compose.yaml` is the same single path
(`/var/lib/dn-notification` -> `/var/lib/dn-notification`), so values inside
the container and on the host stay aligned without any extra wiring.

Use `dnnotification edit-env` to tweak the file after install. The required
keys are:

| Key           | Purpose                                                |
|---------------|--------------------------------------------------------|
| `TG_API_ID`   | From https://my.telegram.org/apps                      |
| `TG_API_HASH` | From https://my.telegram.org/apps                      |
| `TG_PHONE`    | International format, e.g. `+1234567890`               |
| `API_KEY`     | Long random string. Required `X-API-KEY` header value  |

Everything else (`HOST_PORT`, `LOG_LEVEL`, search tunables, …) has sensible
defaults in `.env.example` and can be left as-is.

---

## 6. First-time Telegram login

The first run is **interactive** — Telethon needs the SMS code Telegram sends
to your phone (and your 2FA password, if enabled). The CLI doesn't perform
login for you; do it from a shell on the host:

```bash
# If you have 2FA enabled, also:
# export TG_2FA_PASSWORD="your_cloud_password"

docker compose -f /opt/dn-notification/docker-compose.yaml exec dn-notification \
    python -c "
import asyncio
from app.telegram_client import get_telegram_service
async def main():
    svc = get_telegram_service()
    await svc.start()
    await svc.stop()
asyncio.run(main())
"
```

The `.session` file is created under `/var/lib/dn-notification/session/` and
persists across container restarts. Subsequent boots are non-interactive.

---

## 7. Daily operations

Run `dnnotification` with no arguments for an interactive menu, or call a
subcommand directly:

```text
dnnotification
  1) Install
  2) Up
  3) Down
  4) Restart
  5) Update
  6) Logs
  7) Edit compose
  8) Edit env
  9) Status
  0) Exit
```

### `status`

Reports:

- Docker CLI / compose plugin / daemon reachability
- Project, data, voices paths
- `.env` presence
- Image, container state, started-at, healthcheck, published port
- **Installed version** (read from the container via `docker exec`)
- HTTP probe of `GET /health`

Use it from cron or a monitoring script — exit code is 0 unless docker itself
is missing.

### `version`

Prints the running container's version (read from `/app/VERSION` inside the
container). Falls back to the CLI's own version when the container is not
running.

### `edit` / `edit-env`

Opens `docker-compose.yaml` or `.env` in `nano` (falls back to `vim`, then
`vi`). Uses `sudo` because the files live under `/opt`. Set `$EDITOR` and
`$SUDO_EDITOR` if you want a different one.

---

## 8. Updating the deployment

```bash
sudo dnnotification update
```

The `update` flow is deterministic and safe to re-run:

1. **Read installed version** — `docker exec dn-notification cat /app/VERSION`
   gives the version that is *actually* running, never a guess from the host.
2. **Read latest release** from the GitHub Releases API
   (`https://api.github.com/repos/Digitalweb-ir/dn-notification/releases/latest`).
   The `tag_name` field returns `vX.Y.Z`; the leading `v` is stripped before
   comparison.
3. **Compare**:
   * Equal → print `You already have the latest version: <version>` and exit.
   * Newer → print `A new version is available: <version>` and prompt
     `Do you want to install it? (yes/no)`. Anything other than `yes`/`y`
     exits without changes.
4. **Download deployment files** — `docker-compose.yaml` and `.env.example`
   into `/opt/dn-notification`.
5. **Merge `.env`** with the new `.env.example`:
   * Every existing key/value in `.env` is preserved verbatim.
   * Any new key in `.env.example` not present in `.env` is prompted for
     interactively and appended to `.env`.
   * Re-running the merge is a no-op (idempotent).
6. **Redeploy containers**:
   ```bash
   docker compose down
   docker rmi digitalneetwork/dn-notification:latest    # force a fresh pull
   docker compose pull
   docker compose up -d
   ```

The image is removed before pulling so that the next `pull` is guaranteed to
fetch a fresh copy even if the registry tag wasn't repushed.

---

## 9. Versioning & release flow

The project is versioned and released by [semantic-release](https://github.com/semantic-release/semantic-release),
which is the single source of truth for the release version. Every push to
`main` triggers `.github/workflows/release.yml`, which runs:

1. `test` — installs Python deps and smoke-imports the app.
2. `release` — `npx semantic-release`:
   * Analyzes commits since the last release tag (Conventional Commits).
   * Picks the next semver version.
   * Runs `write-version.sh <version>` (at the repo root), which
     updates the `VERSION` file and the `# Version:` header in
     `app/__init__.py`.
   * Commits those file changes, tags the commit `v<version>`, and
     creates a GitHub Release with auto-generated notes.
3. `docker` — builds and pushes a multi-arch
   (`linux/amd64`, `linux/arm64`) image to
   `digitalneetwork/dn-notification:<version>` and `:latest`.

Conventional Commit prefixes map to release bumps as follows:

| Commit prefix / marker           | Bump   | Example result       |
|----------------------------------|--------|----------------------|
| `BREAKING CHANGE:` (body / footer) | major  | `1.2.3 -> 2.0.0`    |
| `break: …` (legacy alias)        | major  | `1.2.3 -> 2.0.0`     |
| `feat: …` / `feat!: …`           | minor  | `1.2.3 -> 1.3.0`     |
| `fix: …`                         | patch  | `1.2.3 -> 1.2.4`     |
| (no qualifying commit)           | —      | version unchanged    |

The `break:` prefix is a project-specific alias preserved for backward
compatibility with the previous hand-rolled release system; the standard
Conventional Commits marker is `BREAKING CHANGE:` in the commit body.

PRs run semantic-release in `--dry-run` mode and only log the version that
*would* be released. They do not commit, tag, or publish anything.

### CI secrets

Configure these three secrets in the repository's **Settings → Secrets and
variables → Actions** page. They are referenced inline by `release.yml` via
`${{ secrets.X }}`; they are never read from `.env` or any file in the
repository.

| Secret               | Purpose                                                          |
|----------------------|------------------------------------------------------------------|
| `GH_TOKEN`           | PAT or fine-grained token with `contents: write` for the target repo. Pushes the release commit, tag, and GitHub Release. semantic-release specifically needs a token that can push to protected branches — the auto-provisioned `GITHUB_TOKEN` is not sufficient. |
| `DOCKERHUB_USERNAME` | Docker Hub account owning the `digitalneetwork/dn-notification` image. |
| `DOCKERHUB_TOKEN`    | Docker Hub **access token** (not the account password) with `Read, Write, Delete` scope on the image repository. |

The dry-run job on PRs uses the auto-provisioned `secrets.GITHUB_TOKEN`
(read-only is enough for a dry run); the release job uses the custom
`secrets.GH_TOKEN` (write access required for the push).

The `dnnotification.sh update` command reads the public GitHub Releases API
unauthenticated; it picks up `$GITHUB_TOKEN` from the operator's shell
environment if higher rate limits are needed.

### Where the version lives in the code

* The `VERSION` file at the repo root is the on-disk source of truth.
  It is written by `write-version.sh` on every release and baked into
  the Docker image by the `Dockerfile`'s `COPY VERSION ./VERSION`.
* `app/__init__.py` reads `VERSION` at import time and exposes it as
  `__version__`. `app/main.py` uses that constant for the FastAPI
  app's `version=` field.

---

## 10. Backing up and restoring

The **only** state worth backing up lives under `/var/lib/dn-notification`:

```bash
# Back up session + logs (the session is the critical one).
sudo tar -czf dn-backup-$(date +%F).tgz \
    -C /var/lib/dn-notification session logs

# Restore.
sudo systemctl stop dn-notification 2>/dev/null   # if you added a systemd unit
dnnotification down
sudo tar -xzf dn-backup-YYYY-MM-DD.tgz -C /var/lib/dn-notification
dnnotification up
```

Voice files are immutable templates; you can rebuild them from source control
or your own asset bucket, so they're not part of the backup.

---

## 11. Security checklist

- [ ] `/var/lib/dn-notification` is `chmod 700` (the install script does this).
- [ ] `.env` is `chmod 600`.
- [ ] `API_KEY` is a long random string, rotated periodically.
- [ ] `TG_API_ID` / `TG_API_HASH` / `API_KEY` / `*.session` are **not**
      committed to git. The repo's `.gitignore` and `.dockerignore` already
      cover this.
- [ ] The service runs behind a firewall or private network. The API has
      no per-user model — anyone with `X-API-KEY` can drive the account.
- [ ] Container runs as UID 1000 (`svc`) inside, not root.
- [ ] `restart: always` is fine for a personal support bot; if you front
      this with a public reverse proxy, add rate limiting and TLS there.

---

## 12. Files in this directory

| File                       | What it is                                                |
|----------------------------|-----------------------------------------------------------|
| `VERSION`                  | On-disk release semver; written by semantic-release on every release |
| `package.json`             | Minimal Node manifest; pins `semantic-release` for CI     |
| `release.config.cjs`       | semantic-release configuration (plugins, branches, rules) |
| `write-version.sh`        | Updates `VERSION` and `app/__init__.py` for a new release |
| `Dockerfile`               | Production image (Python 3.11 slim, non-root, tini, healthcheck, copies VERSION) |
| `docker-compose.yaml`      | Single-service compose file (pulls pre-built image)      |
| `.env.example`             | Documented env template (copy to `.env`)                 |
| `dnnotification.sh`        | The CLI — install to `/usr/local/bin/dnnotification`     |
| `.github/workflows/`       | CI: semantic-release + Docker image publish on `main`     |

See `../README.md` for application-level docs (API endpoints, n8n usage,
tuning knobs).
