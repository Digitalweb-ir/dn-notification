#!/usr/bin/env bash
# =============================================================================
#  scripts/write-version.sh — write the new release version
# =============================================================================
#  Invoked by @semantic-release/exec (see release.config.cjs). The version
#  argument is the semver string semantic-release just computed for the
#  release (e.g. "1.4.0"). The script writes it to the two locations the
#  project uses as a runtime version source:
#
#    1. ./VERSION              (single on-disk source of truth; baked into
#                               the Docker image by the Dockerfile)
#    2. app/__init__.py        (a fallback `Version: ...` line in the
#                               header, so the version is discoverable
#                               from Python without reading a sibling file
#                               — useful for tooling and editable installs)
#
#  The script is idempotent: running it again with the same version leaves
#  both files unchanged. Running it with no argument prints the current
#  version and exits 0. Exit code is non-zero only on I/O or argument
#  errors.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION_FILE="${REPO_ROOT}/VERSION"
INIT_FILE="${REPO_ROOT}/app/__init__.py"

read_current_version() {
    [[ -f "$VERSION_FILE" ]] || { echo ""; return 0; }
    tr -d '[:space:]' < "$VERSION_FILE"
}

usage() {
    cat <<'EOF'
Usage: write-version.sh <version>
       write-version.sh --print    # print the current VERSION and exit

Arguments:
  <version>   A semver string (e.g. 1.4.0). Written to VERSION and to the
              Version: header in app/__init__.py.
EOF
}

# `--print` mode: just emit the current value from the on-disk file.
if [[ "${1:-}" == "--print" ]]; then
    read_current_version
    exit 0
fi

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 64
fi

version="$1"
if ! [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$ ]]; then
    echo "write-version.sh: '$version' is not a valid semver string" >&2
    exit 65
fi

# 1. Write the repo-root VERSION file. Idempotent: skip if unchanged.
current=$(read_current_version)
if [[ "$current" == "$version" ]]; then
    echo "write-version.sh: VERSION already at $version; no change"
else
    printf '%s\n' "$version" > "$VERSION_FILE"
    echo "write-version.sh: wrote $VERSION_FILE ($current -> $version)"
fi

# 2. Update the Version: header in app/__init__.py. We update a header
#    line of the form `# Version: <x>` so the import-time
#    `Path(...).read_text()` logic in app/__init__.py stays the single
#    runtime source of truth; this header is only a fallback for
#    editable installs and for tooling that parses this file directly.
#    Skip the rewrite if the file is already at the target version —
#    keeps `git status` clean on repeated runs.
if [[ -f "$INIT_FILE" ]]; then
    current_header=$(grep -E '^# *Version:' "$INIT_FILE" 2>/dev/null \
        | sed -E 's/^# *Version:[[:space:]]*//' | tr -d '[:space:]' || true)
    if [[ "$current_header" == "$version" ]]; then
        echo "write-version.sh: Version: header already at $version; no change"
    elif grep -qE '^# *Version:' "$INIT_FILE"; then
        # Header line present but stale — rewrite it.
        tmp=$(mktemp)
        sed -E "s|^# *Version:.*|# Version: ${version}|" "$INIT_FILE" > "$tmp"
        mv "$tmp" "$INIT_FILE"
        echo "write-version.sh: updated Version: header in $INIT_FILE"
    else
        # No header present yet — prepend one.
        tmp=$(mktemp)
        {
            printf '# Version: %s\n' "$version"
            cat "$INIT_FILE"
        } > "$tmp"
        mv "$tmp" "$INIT_FILE"
        echo "write-version.sh: prepended Version: header to $INIT_FILE"
    fi
fi

exit 0
