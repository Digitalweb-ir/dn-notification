# Deployment Guide

Production deployment of the **DN Notification** Telegram MTProto automation
service using Docker Compose, managed by a single CLI: `dnnotification`.

---

## 1. Project layout

The CLI owns two top-level directories on the host. They are separated by
design so that project code and **sensitive session data** can be backed up,
restored, and permissioned independently. **All persistent application data —
including session data — lives under `/var/lib/dn-notification`**; the
container bind-mounts that single directory at the same path
(see `docker-compose.yaml`).

| Path                                | Purpose                                            | Default perms |
|-------------------------------------|----------------------------------------------------|---------------|
| `/opt/dn-notification`              | Project code, `docker-compose.yaml`, `.env`        | `755`         |
| `/var/lib/dn-notification`          | Persistent data — bind-mounted into the container  | `700`         |
| `/var/lib/dn-notification/session`  | Telegram `.session` file (account credential)      | `700`         |
| `/var/lib/dn-notification/logs`     | Application logs                                   | `755`         |

> The `.session` file grants **full access** to the Telegram account that
> signed in. Treat it like a password. The `700` permission on the data
> directory means only root (or a member of root's group) can read it.

> The other sub-paths (`session/`, `logs/`) are derived from
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
   `/var/lib/dn-notification/{session,logs}`.
6. Sets `755` on the data directory tree so the container's root
   process can read and write the bind-mount.
7. Prompts for `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `API_KEY` and writes
   `.env` (mode `600`).
8. Copies itself to `/usr/local/bin/dnnotification` (extension stripped, +x).
9. Pulls `digitalneetwork/dn-notification:latest` and runs
   `docker compose up -d`.

For the local file paths that you must populate by hand before first use:

```bash
```

---

## 5. Configuration

`/opt/dn-notification/.env` is kept deliberately small. The **only** path you
configure is `DATA_DIR` session, and logs are derived from it in
`app/config.py`:

```python
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

The first run is **interactive** — Telethon needs the SMS code Telegram
sends to your phone (and your 2FA password, if enabled). The
operator CLI is the in-container Python CLI (`python -m app.cli`),
exposed on the host via a thin passthrough:

```bash
sudo dnnotification cli tglogin
```

What it does, in order:

1. Brings the container up if it is not already running (the FastAPI
   lifespan will fail without a session, but `docker exec` still
   attaches).
2. Runs `python -m app.cli tglogin` inside the container with a
   TTY attached (`docker exec -it`). Telegram sends a code to your
   Telegram app.
3. The CLI prompts for the code (input is hidden via `getpass` — the
   code is **never echoed to the terminal**, never logged, never
   written to disk). On a typo, it re-prompts without re-sending —
   the original code remains valid until it expires.
4. If the account has 2FA enabled, the CLI prompts for the cloud
   password next (also hidden).
5. On success, the `.session` file is written to
   `/var/lib/dn-notification/session/` on the host bind-mount.
6. The service is restarted automatically so the running container's
   in-memory Telethon client picks up the new session file.

Verify with:

```bash
sudo dnnotification cli status
curl -s http://localhost:8000/health
# {"status": "ok", "telegram_connected": true, "session": "telegram_session"}
```

The `.session` file persists across container restarts and updates;
you only need to re-run `tglogin` if Telegram invalidates the session
(e.g. you signed the account out from another device, or the auth
key expired).

### The `cli` passthrough

`dnnotification cli` is a thin `docker exec` wrapper around the
in-container Python CLI. Run `dnnotification cli` (no arguments) to
see the CLI's own menu / help. Any command the Python CLI adds in
the future (`tglogin`, `status`, future ones like `logout`,
`whoami`, …) is automatically reachable through the wrapper —
the shell script does not need to be touched again.

> **TTY requirement.** The passthrough always allocates a TTY
> (`docker exec -it`) because `tglogin` reads the SMS code and 2FA
> password via `getpass`. That means `dnnotification cli …` cannot
> be driven from a non-interactive shell (cron, scripts). For
> scripted use, call `docker exec -i …` directly.

### Scripted / non-interactive login

For automation, the lifespan still accepts `TG_CODE` and
`TG_2FA_PASSWORD` as container env vars:

```bash
docker compose -f /opt/dn-notification/docker-compose.yaml exec \
    -e TG_CODE=12345 -e TG_2FA_PASSWORD=your_cloud_password \
    dn-notification python -m app.cli tglogin
```

The code still has to be received first and entered immediately
(it is single-use and short-lived), so this is rarely the right
shape — `dnnotification cli tglogin` is.

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
  6) CLI (passthrough to Python CLI)
  7) Logs
  8) Edit compose
  9) Edit env
 10) Status
  0) Exit
```

### `status`

Reports:

- Docker CLI / compose plugin / daemon reachability
- Project, data paths
- `.env` presence
- Image, container state, started-at, healthcheck, published port
- **Installed version** (read from the local image's OCI label via `docker image inspect`, no need for the container to be running)
- HTTP probe of `GET /health`

Use it from cron or a monitoring script — exit code is 0 unless docker itself
is missing.

### `version`

Prints the installed image's version (read from the
`org.opencontainers.image.version` OCI label via `docker image inspect`).
Falls back to the CLI's own version when no image is installed locally.

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

1. **Read installed version** — `docker image inspect --format ... org.opencontainers.image.version`
   gives the version that is *actually* installed locally, without
   requiring the container to be running. The label is stamped on the
   image at build time from the same git tag the GitHub Releases API
   returns, so the two can never drift.
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
which is the single source of truth for the release version: the git tag
(`vX.Y.Z`) is the version. Every push to `main` triggers
`.github/workflows/release.yml`, which runs:

1. `test` — installs Python deps and smoke-imports the app.
2. `release` — `npx semantic-release`:
   * Analyzes commits since the last release tag (Conventional Commits).
   * Picks the next semver version.
   * Tags the commit `v<version>` and creates a GitHub Release with
     auto-generated notes.
   * No repo files are mutated by the release — the previous flow wrote
     `VERSION` and a `__init__.py` header, but that created a
     stale-cache problem: a developer's local checkout would not see
     the change until they pulled, and the next local commit would roll
     the version back to whatever was on disk locally. The tag is the
     only authoritative source.
3. `docker` — on pushes to `main` after a successful release, the
   `release` job's output (populated by
   `@semantic-release/exec`'s `successCmd` from `${nextRelease.version}`)
   feeds the build: `VERSION=<x.y.z>` is passed as a build-arg and
   the same value lands on the
   `org.opencontainers.image.version` OCI label, which is what
   `dnnotification update` compares against the GitHub Releases API.
   The version is propagated via job outputs, NOT via
   `git describe`: the version-propagation pattern recommended by
   the semantic-release docs avoids the `git describe` failure
   modes on shallow clones, missing tags, and first releases.

If `semantic-release` finds no qualifying commits on a push to
`main`, the `release` job exits successfully without publishing
anything, the `successCmd` does not run, and the `docker` job is
skipped automatically (it gates on
`needs.release.outputs.released == 'true'`). This is
semantic-release's documented skip behavior; no separate gate
is needed.

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

* The git tag `vX.Y.Z` is the single source of truth for the version.
* The release workflow passes the tag into the Dockerfile as a
  `VERSION` build-arg. The Dockerfile bakes it into the image in two
  places: the `APP_VERSION` env var and the
  `org.opencontainers.image.version` OCI label.
* `app/__init__.py` resolves `__version__` at import time, preferring
  `APP_VERSION` and falling back to `git describe --tags --dirty` (for
  editable/dev installs) and then `0.0.0+unknown`. `app/main.py` uses
  that constant for the FastAPI app's `version=` field.
* `dnnotification update` reads the installed version from the OCI
  label with `docker image inspect` and the latest version from the
  GitHub Releases API, so the two endpoints of the comparison share
  the same source (the git tag) and can never disagree.

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

---

## 11. Security checklist

- [ ] `/var/lib/dn-notification` is `chmod 755` (the install script does this).
- [ ] `.env` is `chmod 600`.
- [ ] `API_KEY` is a long random string, rotated periodically.
- [ ] `TG_API_ID` / `TG_API_HASH` / `API_KEY` / `*.session` are **not**
      committed to git. The repo's `.gitignore` and `.dockerignore` already
      cover this.
- [ ] The service runs behind a firewall or private network. The API has
      no per-user model — anyone with `X-API-KEY` can drive the account.
- [ ] `restart: always` is fine for a personal support bot; if you front
      this with a public reverse proxy, add rate limiting and TLS there.

---

## 12. Files in this directory

| File                       | What it is                                                |
|----------------------------|-----------------------------------------------------------|
| `package.json`             | Minimal Node manifest; pins `semantic-release` for CI     |
| `release.config.cjs`       | semantic-release configuration (plugins, branches, rules) |
| `Dockerfile`               | Production image (Python 3.11 slim, runs as root, tini, healthcheck); takes VERSION as build-arg, stamps OCI labels |
| `docker-compose.yaml`      | Single-service compose file (pulls pre-built image)      |
| `.env.example`             | Documented env template (copy to `.env`)                 |
| `dnnotification.sh`        | The CLI — install to `/usr/local/bin/dnnotification`     |
| `.github/workflows/`       | CI: semantic-release + Docker image publish on `main`     |

See `../README.md` for application-level docs (API endpoints, n8n usage,
tuning knobs).
