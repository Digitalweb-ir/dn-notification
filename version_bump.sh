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
#  Designed to be invoked locally (e.g. by a commit-msg hook) AND by CI:
#
#      ./version_bump.sh             # apply the implied bump in place
#      ./version_bump.sh --print     # print the bump that WOULD be applied
#      ./version_bump.sh --check     # exit 0 if VERSION matches commits,
#                                    # exit 1 otherwise (use in CI to gate merges)
#      ./version_bump.sh --since TAG # override the diff range
#
#  The script keeps `./VERSION` and `app/__init__.py.__version__` in lockstep;
#  it does NOT create or move git tags. Releases are tagged separately.
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
    # Print the script's docstring: from line 2 to the line BEFORE `set -Eeuo`.
    awk 'NR>1 && /set -Eeuo/{exit} NR>1' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

# -----------------------------------------------------------------------------
# Detect the Conventional Commits prefix (case- and whitespace-tolerant) and
# return the highest bump level in the given range.
#
# Accepted prefixes (case-insensitive):
#   break: ... | break : ...   -> MAJOR
#   feat:  ... | feat  : ...   -> MINOR
#   fix:   ... | fix   : ...   -> PATCH
#
# Tolerates extra spaces and the common "fix :" typo (colon mistakenly placed
# after the space instead of after the keyword).
#
# Returns 0 (BUMP_NONE) / 1 (BUMP_PATCH) / 2 (BUMP_MINOR) / 3 (BUMP_MAJOR).
# -----------------------------------------------------------------------------
detect_bump() {
    local range="$1"
    local bump="$BUMP_NONE"
    local line keyword
    while IFS= read -r line; do
        # Lowercase and trim at most one optional space; the regex below
        # handles whitespace tolerance both before and after the colon.
        keyword=$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]' \
            | sed -nE 's/^(break|feat|fix)[[:space:]]*:[[:space:]]?(.*)$/\1/p')
        case "$keyword" in
            break) bump=$BUMP_MAJOR ;;
            feat)  [[ $bump -lt $BUMP_MAJOR ]] && bump=$BUMP_MINOR ;;
            fix)   [[ $bump -lt $BUMP_MINOR ]] && bump=$BUMP_PATCH ;;
        esac
    done < <(git -C "$SCRIPT_DIR" log --no-merges --pretty=%s "$range" 2>/dev/null || true)
    printf '%d' "$bump"
}

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
write_version() {
    local next="$1"
    local init_file="${SCRIPT_DIR}/app/__init__.py"

    # Idempotent: skip the write if VERSION is already at $next. Keeps `git
    # status` clean on repeated runs and lets --check do a no-op round-trip.
    local current
    current=$(read_version 2>/dev/null || true)
    if [[ "$current" == "$next" && ! -f "$init_file" ]]; then
        return 0
    fi

    printf '%s\n' "$next" > "$VERSION_FILE"
    log "Wrote $VERSION_FILE"

    # Mirror the version into app/__init__.py so Python imports see the same
    # value. Keep this in lockstep with the single source of truth at
    # ./VERSION. Only touches the line beginning with `__version__ =` so it
    # works even if the file is hand-edited.
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

main() {
    local print_only=0
    local check_only=0
    local quiet=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)  usage; exit 0 ;;
            --print)    print_only=1 ;;
            --check)    check_only=1; quiet=1 ;;
            --quiet|-q) quiet=1 ;;
            --since)    shift; SINCE_TAG="${1:-}"; [[ -n "$SINCE_TAG" ]] || die "--since requires a tag/ref." ;;
            *)          die "Unknown argument: $1 (try --help)." ;;
        esac
        shift
    done

    # Per-mode logging
    if [[ $quiet -eq 1 ]]; then
        log()  { :; }
        warn() { printf '[!] %s\n' "$*" >&2; }
    fi

    local current
    current=$(read_version)
    [[ $quiet -eq 0 ]] && log "Current version: $current"

    if [[ -z "$SINCE_TAG" ]]; then
        SINCE_TAG=$(latest_version_tag || true)
        if [[ -n "$SINCE_TAG" ]]; then
            [[ $quiet -eq 0 ]] && log "Auto-detected last version tag: $SINCE_TAG"
        else
            [[ $quiet -eq 0 ]] && log "No prior version tag found — scanning entire history."
        fi
    fi

    local range
    if [[ -n "$SINCE_TAG" ]]; then
        range="${SINCE_TAG}..HEAD"
    else
        range="HEAD"
    fi

    local bump_idx bump_name next expected
    bump_idx=$(detect_bump "$range")
    bump_name="${BUMP_NAMES[$bump_idx]}"

    if [[ "$bump_idx" -eq $BUMP_NONE ]]; then
        if [[ $check_only -eq 1 ]]; then
            # No bump implied, no bump in VERSION — consistent.
            exit 0
        fi
        [[ $quiet -eq 0 ]] && log "No bump keywords (break/feat/fix) found in $range. Version unchanged."
        exit 0
    fi

    # The "expected" version is the last published tag plus the implied bump —
    # NOT the current VERSION plus the implied bump. This is the only way
    # `--check` is idempotent: if the user has already applied the bump,
    # `current == expected` and the check passes; if they haven't, the check
    # fails and tells them the precise version to set.
    local base="$current"
    if [[ -n "$SINCE_TAG" ]]; then
        # Strip the leading 'v' (or 'V') from the tag to get a comparable base.
        base="${SINCE_TAG#[vV]}"
    fi
    expected=$(next_version "$base" "$bump_name")
    # The "next" version we'll print / write is `expected`, which is one bump
    # applied to the last tag. This is what the user-facing `--print` and
    # default-bump modes need to produce.
    next="$expected"
    [[ $quiet -eq 0 ]] && log "Detected '$bump_name' bump -> $next (from $SINCE_TAG)"

    if [[ $print_only -eq 1 ]]; then
        printf '%s\n' "$next"
        exit 0
    fi

    if [[ $check_only -eq 1 ]]; then
        if [[ "$current" == "$next" ]]; then
            # VERSION already reflects the implied bump.
            exit 0
        fi
        printf 'VERSION mismatch: file=%s, expected=%s (bump=%s from %s)\n' \
            "$current" "$next" "$bump_name" "$range" >&2
        exit 1
    fi

    write_version "$next"
}

main "$@"
