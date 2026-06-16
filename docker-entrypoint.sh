#!/usr/bin/env sh
# =============================================================================
#  docker-entrypoint.sh — exec the CMD
# =============================================================================
#  The container runs as root. The bind-mounted $DATA_DIR is owned by
#  root (created by the install script on Linux, or by the Docker
#  Desktop VM on macOS), so root in the container can read and write
#  every persistent path without any chown/gosu dance.
#
#  The entrypoint exists (rather than letting tini be the entrypoint
#  directly) so we have a stable hook for future setup steps (e.g. a
#  one-time migration, or printing a startup banner).
# =============================================================================
exec "$@"
