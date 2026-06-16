# syntax=docker/dockerfile:1.6
# ---------- Stage 1: dependencies ----------
FROM python:3.11-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "uvicorn[standard]==0.30.6"


# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "uvicorn[standard]==0.30.6"

COPY app ./app

# Single source of truth for the image version. Read at runtime by the
# CLI's update flow to compare against the latest published version.
COPY VERSION ./VERSION

# Persistent data lives under /var/lib/dn-notification (configurable
# via DATA_DIR at runtime). The host bind-mounts the same path from
# the host into the container (see docker-compose.yaml), so the
# per-purpose subdirs (session, logs, voices) are NOT created here:
#   * On a fresh install, /var/lib/dn-notification may not exist on
#     the host yet, and a bind-mount over a non-existent host path
#     makes Docker create it as root with restrictive perms.
#   * On an existing install, the host directories may already be
#     populated and we must not clobber their ownership.
# docker-entrypoint.sh owns the layout: it creates, chowns, and
# chmods each subdir at startup, regardless of host state.

# The container runs as root. There is no in-image service user: the
# entrypoint ensures every persistent path is owned by root, so root
# in the container is the natural owner and can read/write every file
# it needs without any chown gymnastics at request time.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# Healthcheck: /health returns 200 once the FastAPI app is up. start-period
# covers the Telethon connect + dialog warm-up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# tini reaps zombies and forwards signals so uvicorn shuts down cleanly.
# The entrypoint sits between tini (PID 1) and the CMD; tini reaps the
# entrypoint after it `exec`s into the CMD, so signal forwarding works
# the same as a direct CMD.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
