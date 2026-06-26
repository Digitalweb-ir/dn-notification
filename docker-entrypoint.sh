#!/usr/bin/env sh
# =============================================================================
#  docker-entrypoint.sh — prepare persistent storage, then exec the CMD
# =============================================================================
#  Responsibilities
#  ----------------
#  1. Make sure every directory the app needs under $DATA_DIR exists and is
#     writable by the user we run as (root in this image).
#  2. Hand off to the CMD with `exec` so signals are forwarded correctly.
#
#  Why this is here, not in Python
#  -------------------------------
#  $DATA_DIR is bind-mounted from the host. The host directory may come up in
#  any state:
#    * absent                  — fresh install on a brand-new host
#    * present, owned by root  — the normal case after `dnnotification install`
#    * present, owned by uid 1000 (or anything else)
#                                — e.g. created by Docker Desktop's VM on macOS,
#                                  a previous run of an older image that used
#                                  USER 1000, or hand-created by the operator
#
#  Handling all of those from inside Python is awkward: it requires
#  PermissionError-aware fallbacks, stat calls, and a chown that only root
#  can do. Doing it once, up front, in the entrypoint means the app can
#  trust its storage layout and fail fast with a clear message if something
#  is genuinely wrong.
# =============================================================================

set -eu

# DATA_DIR is the single source of truth for persistent storage — set in
# docker-compose.yaml. Session and logs are derived from it.
DATA_DIR="${DATA_DIR:-/var/lib/dn-notification}"
SESSION_DIR="${DATA_DIR}/session"
LOGS_DIR="${DATA_DIR}/logs"

# The app runs as root, so we want every directory to be owned by root.
# 0755 = rwx for owner (root), r-x for everyone else — safe for bind-mounts
# on multi-user hosts while still letting the app write.
TARGET_OWNER="root"
TARGET_MODE="0755"

log() {
    printf '[entrypoint] %s\n' "$*"
}

# chown that works on BusyBox/Alpine, GNU, and BSD. `--` makes it safe even
# if $DATA_DIR starts with a dash (it doesn't, but cheap defense).
safe_chown() {
    if chown --help 2>&1 | grep -q -- '--no-dereference'; then
        chown --no-dereference "$@"
    else
        chown "$@"
    fi
}

# Ensure a directory exists, is owned by $TARGET_OWNER, and has mode
# $TARGET_MODE. Handles three host states:
#   1. path absent          -> mkdir -p
#   2. path is a directory  -> keep, fix ownership + mode
#   3. path is a regular file (or symlink) -> fatal, we cannot recover
ensure_dir() {
    dir="$1"

    if [ ! -e "$dir" ] && [ ! -L "$dir" ]; then
        log "creating $dir"
        mkdir -p "$dir"
    fi

    if [ ! -d "$dir" ]; then
        log "ERROR: $dir exists but is not a directory."
        log "       Refusing to continue — fix the bind mount and retry."
        exit 1
    fi

    # chown the directory itself. Do NOT recurse (-R is intentionally
    # avoided): on an existing install, the user may have populated
    # $DATA_DIR with their own session file or logs,
    # and we must not change ownership underneath them. The container
    # only needs to be able to *write new* files (session) and *append* to
    # logs, and root can do all of that without
    # owning the pre-existing content.
    #
    # chown is best-effort: a host directory on a filesystem that
    # doesn't support ownership changes (e.g. a noowners mount, or a
    # bind-mount into a container that has already remapped the uid)
    # will reject it. The write-probe below is the real check — if the
    # container can actually write to the directory, ownership is
    # irrelevant.
    if ! safe_chown "$TARGET_OWNER" "$dir" 2>/dev/null; then
        log "warning: could not chown $dir to $TARGET_OWNER (filesystem may not support it); continuing"
    fi
    if ! chmod "$TARGET_MODE" "$dir" 2>/dev/null; then
        log "warning: could not chmod $dir (filesystem may not support it); continuing"
    fi
}

log "Preparing persistent storage under $DATA_DIR"
for d in "$DATA_DIR" "$SESSION_DIR" "$LOGS_DIR"; do
    ensure_dir "$d"
done

# Smoke test: confirm we can actually write into the bind mount. This
# catches the rare case where chown silently succeeded (e.g. a read-only
# bind mount layered on top) but writes are still denied. We use a
# uniquely-named temp file in each dir and clean up.
for d in "$SESSION_DIR" "$LOGS_DIR"; do
    probe="$d/.dn-notification-write-probe.$$"
    if ! (umask 077 && : > "$probe" && rm -f "$probe") 2>/dev/null; then
        log "ERROR: $d is not writable by the container (uid=$(id -u))."
        log "       This is almost always a bind-mount issue:"
        log "         - the host directory may be owned by a different user,"
        log "         - the host directory may be on a read-only mount,"
        log "         - or a USER directive in compose may be overriding root."
        log "       Fix on the host, e.g.:  sudo chown -R root:root $d"
        exit 1
    fi
done

# Derive uvicorn's --log-level from the DEBUG env var so that uvicorn's
# own startup messages (before the app lifespan runs) respect the same
# setting.  Without this, uvicorn always starts at INFO and overrides
# the root logger — our in-app dictConfig only catches up later.
if [ "${DEBUG:-false}" = "true" ]; then
    UVICORN_LOG_LEVEL=debug
else
    UVICORN_LOG_LEVEL=info
fi
export UVICORN_LOG_LEVEL

log "Storage ready. Starting application (LOG_LEVEL=$UVICORN_LOG_LEVEL)."
exec "$@"
