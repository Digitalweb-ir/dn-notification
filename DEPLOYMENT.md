# Deployment Guide

Production deployment of the **DN Notification** Telegram MTProto automation
service using Docker Compose, managed by a single CLI: `dnnotification`.

---

## 1. Host layout

The script owns two directories on the host. They are separate by design so
that project code and **sensitive session data** can be backed up, restored,
and permissioned independently. **All persistent application data — including
voice templates — lives under `/var/lib/dn-notification`**; the container
bind-mounts that single directory at `/data` (see `docker-compose.yaml`).

| Path                                | Purpose                                          | Default perms |
|-------------------------------------|--------------------------------------------------|---------------|
| `/opt/dn-notification`              | Project code, `docker-compose.yaml`, `.env`      | `755`         |
| `/var/lib/dn-notification`          | Persistent data — bind-mounted into the container as `/data` | `700` |
| `/var/lib/dn-notification/session`  | Telegram `.session` file (account credential)    | `700`         |
| `/var/lib/dn-notification/logs`     | Application logs                                 | `755`         |
| `/var/lib/dn-notification/voices`   | Voice templates (`.ogg` files)                   | `755`         |

> The `.session` file grants **full access** to the Telegram account that
> signed in. Treat it like a password. The `700` permission on the data
> directory means only root (or a member of root's group) can read it.

All paths can be overridden by setting environment variables before invoking
`dnnotification`:

```bash
export PROJECT_DIR=/srv/dn-notification
export DATA_DIR=/srv/dn-data
export VOICES_DIR=/srv/dn-data/voices
dnnotification install
```

---

## 2. One-time host setup

### 2.1 Install Docker

The CLI does **not** auto-install Docker. It detects what's missing and prints
install instructions for your OS. Pick one:

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

### 2.2 Install the CLI

Copy `deploy/dnotification` to a directory on your `$PATH`:

```bash
sudo install -m 0755 deploy/dnotification /usr/local/bin/dnotification
dnotification version       # -> dnotification 1.0.0
```

---

## 3. First-time install

```bash
# Pull the project to /opt/dn-notification (or copy files there manually).
sudo git clone <repo-url> /opt/dn-notification

# Edit .env with your Telegram credentials.
sudo cp /opt/dn-notification/.env.example /opt/dn-notification/.env
sudo chmod 600 /opt/dn-notification/.env
sudo $EDITOR /opt/dn-notification/.env
#   TG_API_ID, TG_API_HASH, TG_PHONE, API_KEY  -> required
#   HOST_PORT                                  -> host port (default 8000)

# Add at least one voice template.
sudo cp my-welcome.ogg /opt/dn-notification/voices/welcome.ogg

# Run the install.
dnotification install
```

`dnotification install`:

1. Verifies Docker + compose plugin are reachable.
2. Creates `/opt/dn-notification`, `/var/lib/dn-notification/{session,logs}`,
   and `/opt/dn-notification/voices`.
3. Sets `700` on the data directory, `chown 1000:1000` so the container's
   non-root user can write to the bind-mounts.
4. Tightens `.env` permissions to `600`.
5. Runs `docker compose up -d --build` and shows status.

---

## 4. First-time Telegram login

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

## 5. Daily operations

Run `dnotification` with no arguments for an interactive menu, or call a
subcommand directly:

```text
dnotification
  1) Install        dnotification install
  2) Up             dnotification up
  3) Down           dnotification down
  4) Restart        dnotification restart
  5) Logs           dnotification logs
  6) Edit compose   dnotification edit
  7) Edit env       dnotification edit-env
  8) Status         dnotification status
  0) Exit
```

### `status`

Reports:

- Docker CLI / compose plugin / daemon reachability
- Project, data, voices paths
- `.env` presence
- Container state, started-at, healthcheck, published port
- HTTP probe of `GET /health`

Use it from cron or a monitoring script — exit code is 0 unless docker itself
is missing.

### `edit` / `edit-env`

Opens `docker-compose.yaml` or `.env` in `nano` (falls back to `vim`, then
`vi`). Uses `sudo` because the files live under `/opt`. Set `$EDITOR` and
`$SUDO_EDITOR` if you want a different one — currently the script picks
nano/vim/vi in that order.

---

## 6. Updating the deployment

```bash
cd /opt/dn-notification
sudo git pull                                # pull the latest code
dnotification restart                        # or: dnotification down && dnotification up
```

If you changed `docker-compose.yaml` or the `Dockerfile`:

```bash
dnotification down
dnotification up --build
# (`dnotification up` accepts compose flags since it just calls
#  `docker compose up -d` — pass `--build` or `--force-recreate` as needed.)
```

> Note: `dnotification up` itself only forwards extra args through `cmd_up`.
> For a one-off rebuild, run `cd /opt/dn-notification && docker compose up -d
> --build` directly.

---

## 7. Backing up and restoring

The **only** state worth backing up lives in `/var/lib/dn-notification`:

```bash
# Back up session + logs (the session is the critical one).
sudo tar -czf dn-backup-$(date +%F).tgz \
    -C /var/lib/dn-notification session logs

# Restore.
sudo systemctl stop dn-notification 2>/dev/null   # if you added a systemd unit
dnotification down
sudo tar -xzf dn-backup-YYYY-MM-DD.tgz -C /var/lib/dn-notification
dnotification up
```

Voice files are immutable templates; you can rebuild them from source control
or your own asset bucket, so they're not part of the backup.

---

## 8. Security checklist

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

## 9. Files in this directory

| File                  | What it is                                  |
|-----------------------|---------------------------------------------|
| `Dockerfile`          | Production image (Python 3.11 slim, non-root, tini, healthcheck) |
| `docker-compose.yaml` | Single-service compose file                 |
| `.env.example`        | Documented env template (copy to `.env`)    |
| `dnotification`       | The CLI — install to `/usr/local/bin/`      |

See `../README.md` for application-level docs (API endpoints, n8n usage,
tuning knobs).