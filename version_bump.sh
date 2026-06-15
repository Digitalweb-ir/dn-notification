#!/usr/bin/env bash
# =============================================================================
#  version_bump.sh — VERSION file manager for the dn-notification project
# =============================================================================
#  Reads the current version from the VERSION file in the repo root, inspects
#  commit messages since the last version tag, picks the highest-priority bump
#  keyword (break > feat > fix), and writes the new version back.
#
#  Conventions (Conventional Commits style — the leading keyword decides the
#  bump type; everything after the colon is free-form):
#
#      break: ...   -> MAJOR bump   (e.g. 1.2.3 -> 2.0.0)
#      feat:  ...   -> MINOR bump   (e.g. 1.2.3 -> 1.3.0)
#      fix:   ...   -> PATCH bump   (e.g. 1.2.3 -> 1.2.4)
#
#  Idempotent: running the script with no new qualifying commits leaves the
#  version untouched. Exit code is 0 on a successful (or no-op) run, 1 on
#  any I/O or parse failure.
#
#  Designed to be invoked by CI:
#
#      ./version_bump.sh            # bumps in place, prints new version
#      ./version_bump.sh --print    # prints the bump that WOULD be applied
#      ./version_bump.sh --since TAG  # override the diff range
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${VERSION_FILE:-${SCRIPT_DIR}/VERSION}"
SINCE_TAG="${SINCE_TAG:-}"  # auto-detected from the latest version tag if empty

# Bump priority: highest wins. `break` > `feat` > `fix`.
BUMP_NONE=0
BUMP_PATCH=1
BUMP_MINOR=2
BUMP_MAJOR=3
BUMP_NAMES=(none patch minor major)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
log()  { printf '[i] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,/^# ====/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# Read the current version, strip surrounding whitespace, validate shape.
read_version() {
    [[ -f "$VERSION_FILE" ]] || die "VERSION file not found at $VERSION_FILE."
    local v
    v=$(tr -d '[:space:]' < "$VERSION_FILE")
    [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "VERSION is not semver (got: '$v')."
    printf '%s' "$v"
}

# Print the next version given current=$1 and bump=$2 (one of BUMP_NAMES).
next_version() {
    local current="$1" bump="$2"
    local major minor patch
    IFS='.' read -r major minor patch <<<"$current"
    case "$bump" in
        major) printf '%d.%d.%d' "$((major + 1))" 0 0 ;;
        minor) printf '%d.%d.%d' "$major" "$((minor + 1))" 0 ;;
        patch) printf '%d.%d.%d' "$major" "$minor" "$((patch + 1))" ;;
        *)     printf '%s' "$current" ;;
    esac
}

# Resolve the most recent version tag (vX.Y.Z or X.Y.Z) reachable from HEAD.
latest_version_tag() {
    git -C "$SCRIPT_DIR" tag --list 'v*.*.*' '*.*.*' --sort=-version:refname \
        | head -n1
}

# Inspect every commit message in the given range and return the highest
# bump level found. Range syntax: "TAG..HEAD" or "HEAD~N..HEAD".
detect_bump() {
    local range="$1"
    local bump="$BUMP_NONE"
    local line
    while IFS= read -r line; do
        # Match a Conventional Commits prefix at the start of the subject.
        case "$line" in
            "break:"*|"BREAK:"*) bump=$BUMP_MAJOR ;;
            "feat:"*|"FEAT:"*)
                [[ $bump -lt $BUMP_MAJOR ]] && bump=$BUMP_MINOR
                ;;
            "fix:"*|"FIX:"*)
                [[ $bump -lt $BUMP_MINOR ]] && bump=$BUMP_PATCH
                ;;
        esac
    done < <(git -C "$SCRIPT_DIR" log --no-merges --pretty=%s "$range" 2>/dev/null || true)
    printf '%d' "$bump"
}

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
main() {
    local print_only=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help) usage; exit 0 ;;
            --print)   print_only=1 ;;
            --since)   shift; SINCE_TAG="${1:-}"; [[ -n "$SINCE_TAG" ]] || die "--since requires a tag/ref." ;;
            *)         die "Unknown argument: $1 (try --help)." ;;
        esac
        shift
    done

    local current
    current=$(read_version)
    log "Current version: $current"

    if [[ -z "$SINCE_TAG" ]]; then
        SINCE_TAG=$(latest_version_tag || true)
        if [[ -n "$SINCE_TAG" ]]; then
            log "Auto-detected last version tag: $SINCE_TAG"
        else
            log "No prior version tag found — scanning entire history."
        fi
    fi

    local range
    if [[ -n "$SINCE_TAG" ]]; then
        range="${SINCE_TAG}..HEAD"
    else
        range="HEAD"
    fi

    local bump_idx
    bump_idx=$(detect_bump "$range")
    local bump_name="${BUMP_NAMES[$bump_idx]}"

    if [[ "$bump_idx" -eq $BUMP_NONE ]]; then
        log "No bump keywords (break/feat/fix) found in $range. Version unchanged."
        exit 0
    fi

    local next
    next=$(next_version "$current" "$bump_name")
    log "Detected '$bump_name' bump -> $next"

    if [[ $print_only -eq 1 ]]; then
        printf '%s\n' "$next"
        exit 0
    fi

    printf '%s\n' "$next" > "$VERSION_FILE"
    log "Wrote $VERSION_FILE"

    # Mirror the version into app/__init__.py so Python imports see the same
    # value. Keep this in lockstep with the single source of truth at
    # ./VERSION. Only touches the line beginning with `__version__ =` so it
    # works even if the file is hand-edited.
    local init_file="${SCRIPT_DIR}/app/__init__.py"
    if [[ -f "$init_file" ]]; then
        if grep -q '^__version__' "$init_file"; then
            sed -i.bak -E "s|^__version__ *= *\"[^\"]*\"|__version__ = \"$next\"|" "$init_file"
            rm -f "${init_file}.bak"
        else
            printf '\n__version__ = "%s"\n' "$next" >> "$init_file"
        fi
        log "Synced __version__ in $init_file"
    fi
}

main "$@"
