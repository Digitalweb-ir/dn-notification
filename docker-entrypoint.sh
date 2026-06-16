#!/usr/bin/env bash
# =============================================================================
#  docker-entrypoint.sh — fix DATA_DIR ownership, then exec the CMD
# =============================================================================
#  The container ships with a non-root service user (`svc`, uid 1000) so the
#  application never runs as root. But $DATA_DIR is a host bind-mount, and
#  bind-mounts override whatever ownership the image baked in. If the host
#  directory was created by an earlier install, by root, or by Docker itself,
#  its ownership inside the container is root:root, and the service user
#  cannot traverse or write to it.
#
#  This entrypoint runs as root, ensures $DATA_DIR exists, and recursively
#  chowns it to the in-container service user. It then `exec`s the CMD as
#  that user via `gosu` (which is the standard privilege-drop tool for
#  containers — it does not spawn a shell, so signals and tini work the
#  same as a direct ENTRYPOINT).
#
#  Idempotency:
#    * $DATA_DIR missing  -> mkdir -p, chown to svc
#    * $DATA_DIR present, owned by svc -> no-op
#    * $DATA_DIR present, owned by anything else -> chown -R to svc
#    * Re-running on every container start is safe.
#
#  The chown is scoped strictly to $DATA_DIR. We never walk outside of it.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

# Inherit DATA_DIR from the environment, with the same default compose
# sets in docker-compose.yaml. This keeps the entrypoint usable in
# contexts that don't go through compose (e.g. `docker run`).
: "${DATA_DIR:=/var/lib/dn-notification}"

# The service user is created in the Dockerfile and matches the host
# bind-mount ownership we want to enforce (uid 1000, gid 1000).
SVC_USER="svc"
SVC_UID="$(id -u "$SVC_USER")"
SVC_GID="$(id -g "$SVC_USER")"

# Make sure $DATA_DIR exists and is owned by the service user. `mkdir -p`
# is a no-op if the bind-mount already points at an existing host path.
mkdir -p "$DATA_DIR"
chown -R "$SVC_UID:$SVC_GID" "$DATA_DIR"

# Drop privileges and exec the CMD. `exec` replaces this shell process
# so tini (PID 1 in the container) sees the real CMD as PID 1 and
# signal forwarding (SIGTERM, SIGINT) works as expected. Use the full
# path to gosu so this entrypoint works regardless of $PATH.
exec /usr/sbin/gosu "$SVC_USER" "$@"
