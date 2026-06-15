#!/usr/bin/env bash
# =============================================================================
#  dnnotification.sh — DN Notification manager CLI
# =============================================================================
#  Manages the dockerized DN Notification (Telegram MTProto automation) service.
#
#  Source repo (used to fetch docker-compose.yaml / .env.example / VERSION):
#      https://github.com/Digitalweb-ir/dn-notification  (branch: main)
#
#  Docker image:
#      digitalneetwork/dn-notification:latest
#
#  Host layout (paths are hardcoded):
#      /opt/dn-notification           project files (docker-compose.yaml, .env)
#      /var/lib/dn-notification       ALL persistent data (bind-mounted in)
#          ├── session/   Telegram .session file (account credential)
#          ├── logs/      application logs
#          └── voices/    voice templates (.ogg)
#
#  Usage:
#      dnnotification                 -> interactive menu
#      dnnotification <command>       -> run a command
#      dnnotification help            -> list commands
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

# -----------------------------------------------------------------------------
# Constants — single source of truth for repo, branch, and image.
# If you fork the project, edit these three values and nothing else.
# -----------------------------------------------------------------------------
readonly GIT_REPO="https://github.com/Digitalweb-ir/dn-notification"
readonly GIT_BRANCH="main"
readonly RAW_BASE="https://raw.githubusercontent.com/Digitalweb-ir/dn-notification/${GIT_BRANCH}"
readonly DOCKER_IMAGE="digitalneetwork/dn-notification:latest"
readonly CONTAINER_VERSION_FILE="/app/VERSION"  # baked into the image (Dockerfile)

# -----------------------------------------------------------------------------
# Filesystem layout
# -----------------------------------------------------------------------------
readonly PROJECT_DIR="/opt/dn-notification"
readonly DATA_DIR="/var/lib/dn-notification"
readonly VOICES_DIR="${DATA_DIR}/voices"
readonly SESSION_DIR="${DATA_DIR}/session"
readonly LOGS_DIR="${DATA_DIR}/logs"
readonly COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yaml"
readonly ENV_FILE="${PROJECT_DIR}/.env"
readonly ENV_EXAMPLE="${PROJECT_DIR}/.env.example"

# Source script (this file). BASH_SOURCE is a bash-only array that is only
# populated when the script is loaded from a file. When invoked via stdin
# (e.g. `curl ... | sudo bash -s -- install`), BASH_SOURCE[0] is unset, and
# `set -u` would abort the script. In that case we leave SCRIPT_PATH empty and
# install_cli() self-heals by re-fetching a copy from the canonical repo.
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
else
    SCRIPT_PATH=""
fi
readonly INSTALL_BIN_DIR="/usr/local/bin"
readonly INSTALL_BIN_NAME="dnnotification"   # no extension
readonly INSTALL_BIN_PATH="${INSTALL_BIN_DIR}/${INSTALL_BIN_NAME}"

readonly SERVICE_NAME="dn-notification"
readonly SCRIPT_NAME="dnnotification"
readonly SCRIPT_VERSION="2.1.0"

# -----------------------------------------------------------------------------
# Colors (auto-disabled if stdout isn't a TTY or NO_COLOR is set).
# -----------------------------------------------------------------------------
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
fi

# -----------------------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------------------
log_info()    { printf '%s[i]%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
log_ok()      { printf '%s[OK]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
log_warn()    { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
log_err()     { printf '%s[X]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
log_section() { printf '\n%s== %s ==%s\n' "$C_BOLD$C_CYAN" "$*" "$C_RESET"; }

die()  { log_err "$*"; exit 1; }
silent() { "$@" 2>/dev/null || true; }
trap 'log_err "Aborted by signal."; exit 130' INT TERM

# -----------------------------------------------------------------------------
# Privilege handling
# -----------------------------------------------------------------------------
have_sudo() { command -v sudo >/dev/null 2>&1; }
as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    elif have_sudo; then
        sudo "$@"
    else
        die "This action requires root. Re-run as root or install sudo."
    fi
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "This command must be run as root (try: sudo $SCRIPT_NAME $*)."
    fi
}

# -----------------------------------------------------------------------------
# Docker install / check
# -----------------------------------------------------------------------------
install_docker() {
    log_section "Installing Docker"
    log_info "Running the official Docker install script (get.docker.com)…"
    if have_curl; then
        curl -fsSL https://get.docker.com | sh
    else
        die "curl is required to install Docker. Install curl and re-run."
    fi
    log_ok "Docker installation finished."
}

ensure_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_warn "docker is not installed."
        if [[ -t 0 ]] && ask_yes_no "Install Docker now via the official get.docker.com script?"; then
            install_docker
        else
            die "Docker is required. Re-run after installing it manually."
        fi
    fi
    if ! command -v docker >/dev/null 2>&1; then
        die "docker is still not on PATH after install. Check the installer output above."
    fi
    if ! docker compose version >/dev/null 2>&1; then
        log_err "docker compose plugin (v2) is not installed."
        cat <<EOF
  Install it:
    Debian/Ubuntu : sudo apt-get install -y docker-compose-plugin
    CentOS/RHEL   : sudo yum install -y docker-compose-plugin
    macOS         : bundled with Docker Desktop
    Alpine        : apk add docker-compose
EOF
        die "Install the docker compose v2 plugin and re-run."
    fi
    if ! docker info >/dev/null 2>&1; then
        log_err "Cannot communicate with the Docker daemon."
        cat <<EOF
  Either:
    - Start it:  sudo systemctl start docker
    - Add your user to the docker group:
        sudo usermod -aG docker \$USER   (then log out and back in)
EOF
        die "Docker daemon is unreachable. Fix the above and re-run."
    fi
}

check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_err "docker is not installed."
        cat <<EOF
  Install instructions:
    Debian/Ubuntu : https://docs.docker.com/engine/install/debian/
    macOS         : https://docs.docker.com/desktop/mac/
    Alpine        : apk add docker docker-compose
    One-liner     : curl -fsSL https://get.docker.com | sh
EOF
        return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        log_err "docker compose plugin is not installed (docker compose v2)."
        return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        log_err "Cannot communicate with the Docker daemon."
        return 1
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
have_curl() { command -v curl >/dev/null 2>&1; }

ask_yes_no() {
    local prompt="$1"
    local reply
    while true; do
        read -r -p "$prompt [y/N]: " reply
        case "${reply,,}" in
            y|yes) return 0 ;;
            n|no|"") return 1 ;;
            *) printf "Please answer y or n.\n" ;;
        esac
    done
}

prompt_required() {
    local label="$1"
    local value
    while true; do
        read -r -p "$label: " value
        if [[ -n "$value" ]]; then
            printf '%s' "$value"
            return 0
        fi
        log_warn "Value cannot be empty."
    done
}

# Parse "X.Y.Z" into a comparable integer tuple.
semver_tuple() {
    local v="$1"
    [[ "$v" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || { echo "0 0 0"; return 1; }
    echo "$((BASH_REMATCH[1])) $((BASH_REMATCH[2])) $((BASH_REMATCH[3]))"
}

# Return 0 if $1 < $2, 1 otherwise (both semver).
semver_lt() {
    local a b
    read -r -a a < <(semver_tuple "$1")
    read -r -a b < <(semver_tuple "$2")
    for i in 0 1 2; do
        if (( a[i] < b[i] )); then return 0; fi
        if (( a[i] > b[i] )); then return 1; fi
    done
    return 1
}

# -----------------------------------------------------------------------------
# Layout / permissions
# -----------------------------------------------------------------------------
ensure_layout() {
    log_section "Ensuring directories"
    as_root mkdir -p "$PROJECT_DIR"
    as_root mkdir -p "$DATA_DIR" "$SESSION_DIR" "$LOGS_DIR" "$VOICES_DIR"

    # The data directory holds the Telegram session (full account access if
    # leaked), so lock it down. Voices and logs are world-readable.
    as_root chmod 700 "$DATA_DIR"
    as_root chmod 700 "$SESSION_DIR"
    as_root chmod 755 "$LOGS_DIR"
    as_root chmod 755 "$VOICES_DIR"

    # Hand the bind-mount to UID 1000:1000 so the in-container service user
    # (Dockerfile: `useradd --uid 1000 svc`, compose: `user: "1000:1000"`) can
    # write to it. We do this unconditionally — `chown` operates on the
    # bind-mounted directory contents, it does NOT require a UID 1000 user
    # to exist on the host. Skipping this when the host has no UID 1000 user
    # leaves the dir owned by root with mode 700, and the container process
    # then crashes with PermissionError on the first `mkdir` / `stat`.
    as_root chown -R 1000:1000 "$DATA_DIR"
    log_ok "Project dir: $PROJECT_DIR"
    log_ok "Data dir:    $DATA_DIR"
    log_ok "  ├─ session: $SESSION_DIR"
    log_ok "  ├─ logs:    $LOGS_DIR"
    log_ok "  └─ voices:  $VOICES_DIR"
}

# -----------------------------------------------------------------------------
# Compose helper — every lifecycle command goes through this.
# -----------------------------------------------------------------------------
compose() {
    ( cd "$PROJECT_DIR" && docker compose "$@" )
}

# -----------------------------------------------------------------------------
# Install CLI to /usr/local/bin/dnnotification (extension stripped, +x).
# Idempotent — re-running just refreshes the file in place.
# -----------------------------------------------------------------------------
install_cli() {
    log_section "Install CLI"

    # Self-install needs a copy of the script on disk. When invoked as
    # `curl ... | sudo bash -s -- install` there is no source file (BASH_SOURCE
    # is unset), so re-fetch the script from the canonical repo into a temp
    # file and use that as the install source.
    if [[ -z "$SCRIPT_PATH" || ! -f "$SCRIPT_PATH" ]]; then
        log_info "Script source not on disk; fetching a fresh copy for self-install."
        have_curl || die "curl is required to fetch the script for self-install."
        local fetched
        fetched=$(mktemp) || die "Could not create temp file for self-install."
        # shellcheck disable=SC2064  # we want $fetched captured NOW, not at trap time.
        trap "rm -f '$fetched'" RETURN
        curl -fsSL --retry 3 -o "$fetched" "${RAW_BASE}/${SCRIPT_NAME}.sh" \
            || die "Failed to download ${RAW_BASE}/${SCRIPT_NAME}.sh."
        chmod 0755 "$fetched"
        SCRIPT_PATH="$fetched"
    fi

    as_root install -d -m 0755 "$INSTALL_BIN_DIR"
    as_root install -m 0755 "$SCRIPT_PATH" "$INSTALL_BIN_PATH"

    if [[ -x "$INSTALL_BIN_PATH" ]]; then
        log_ok "Installed: $INSTALL_BIN_PATH"
    else
        die "Install appeared to succeed but $INSTALL_BIN_PATH is not executable."
    fi

    if command -v "$INSTALL_BIN_NAME" >/dev/null 2>&1; then
        log_ok "On PATH: $(command -v "$INSTALL_BIN_NAME")"
    else
        log_warn "$INSTALL_BIN_DIR is not on \$PATH for this shell."
        log_warn "Add it:  export PATH=\"$INSTALL_BIN_DIR:\$PATH\""
    fi
}

# -----------------------------------------------------------------------------
# Download deployment files from the GitHub repo
# -----------------------------------------------------------------------------
fetch_from_repo() {
    local relpath="$1" dest="$2"
    local url="${RAW_BASE}/${relpath}"
    log_info "Downloading $url"
    have_curl || die "curl is required to fetch deployment files."
    if ! curl -fsSL --retry 3 -o "$dest" "$url"; then
        die "Failed to download $relpath from $url — check your network."
    fi
    log_ok "Wrote $dest"
}

download_deployment_files() {
    log_section "Downloading deployment files"
    as_root install -d -m 0755 "$PROJECT_DIR"
    fetch_from_repo "docker-compose.yaml" "$COMPOSE_FILE"
    fetch_from_repo ".env.example"        "$ENV_EXAMPLE"
    as_root chmod 644 "$COMPOSE_FILE" "$ENV_EXAMPLE"
}

# -----------------------------------------------------------------------------
# .env generation and merging
# -----------------------------------------------------------------------------

# Extract every "KEY=..." (KEY=^[A-Za-z_][A-Za-z0-9_]*$) line from a file.
# Emits "KEY|VALUE" pairs, in file order, with comments and blank lines
# stripped. Empty values are preserved.
parse_env_file() {
    local file="$1"
    [[ -f "$file" ]] || return 0
    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip leading whitespace, ignore blanks/comments.
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Match KEY=VALUE.
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            printf '%s|%s\n' "$key" "$val"
        fi
    done < "$file"
}

# Build a list of "KEY|DEFAULT" pairs from .env.example: every KEY that the
# template documents, paired with its example default (often a placeholder).
parse_env_example() {
    parse_env_file "$1"
}

# Write $key=$val to $ENV_FILE, creating/overwriting just that line.
# Preserves all other content of the file.
upsert_env_var() {
    local file="$1" key="$2" val="$3"
    local tmp
    tmp=$(mktemp)
    # If the key already exists, replace its line; else append.
    if grep -qE "^${key}=" "$file" 2>/dev/null; then
        # Use a delimiter unlikely to appear in user input.
        awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k {$2=v; print; next} {print}' \
            "$file" > "$tmp"
    else
        cat "$file" > "$tmp"
        printf '%s=%s\n' "$key" "$val" >> "$tmp"
    fi
    mv "$tmp" "$file"
}

generate_env_file() {
    log_section "Configure environment"
    log_info "Prompts cannot be skipped — required values only. You can"
    log_info "fine-tune other settings later with: $SCRIPT_NAME edit-env"

    [[ -f "$ENV_EXAMPLE" ]] || die "Missing template $ENV_EXAMPLE — re-run install."

    local tg_api_id tg_api_hash tg_phone api_key
    tg_api_id=$(prompt_required "Telegram API ID (https://my.telegram.org/apps)")
    tg_api_hash=$(prompt_required "Telegram API hash")
    tg_phone=$(prompt_required "Telegram phone (international format, e.g. +1234567890)")
    api_key=$(prompt_required "API_KEY (long random string for X-API-KEY auth)")

    local content
    content=$(awk \
        -v id="$tg_api_id" \
        -v hash="$tg_api_hash" \
        -v phone="$tg_phone" \
        -v key="$api_key" '
        /^TG_API_ID=/  { sub(/=.*/, "=" id);   print; next }
        /^TG_API_HASH=/{ sub(/=.*/, "=" hash); print; next }
        /^TG_PHONE=/   { sub(/=.*/, "=" phone); print; next }
        /^API_KEY=/    { sub(/=.*/, "=" key);   print; next }
        { print }
    ' "$ENV_EXAMPLE")

    as_root install -d -m 0755 "$PROJECT_DIR"
    printf '%s\n' "$content" | as_root tee "$ENV_FILE" >/dev/null
    as_root chmod 600 "$ENV_FILE"
    log_ok "Wrote $ENV_FILE (mode 600)"
}

# Merge an existing .env with a fresh .env.example. The result keeps every
# KEY=VALUE already in .env (no overwrites) and, for any KEY in the new
# template that .env does not contain, prompts the user interactively for a
# value and appends it.
#
# If no .env exists, behaves like an interactive install (subset of
# generate_env_file): prompts for every key in the template.
merge_env_file() {
    log_section "Merging .env with new .env.example"

    if [[ ! -f "$ENV_FILE" ]]; then
        log_warn "No existing $ENV_FILE — running first-time setup."
        generate_env_file
        return
    fi
    if [[ ! -f "$ENV_EXAMPLE" ]]; then
        log_warn "No $ENV_EXAMPLE to merge against — keeping existing .env untouched."
        return
    fi

    # Use a temp file as a "set of existing keys" so we don't need bash 4+
    # associative arrays. Each line is one existing key.
    local keyset
    keyset=$(mktemp)
    parse_env_file "$ENV_FILE" | awk -F'|' '{print $1}' > "$keyset"

    local added=0 skipped=0 line key val new_val
    while IFS='|' read -r key val; do
        [[ -z "$key" ]] && continue
        if grep -qxF "$key" "$keyset"; then
            skipped=$((skipped + 1))
            continue
        fi
        log_info "New key in .env.example: $key (current default: ${val:-<empty>})"
        new_val=$(prompt_required "  value for $key")
        upsert_env_var "$ENV_FILE" "$key" "$new_val"
        # Update the keyset so later iterations see the key as present
        # (protects against duplicate keys in the same template).
        printf '%s\n' "$key" >> "$keyset"
        added=$((added + 1))
    done < <(parse_env_example "$ENV_EXAMPLE")

    rm -f "$keyset"
    as_root chmod 600 "$ENV_FILE"
    log_ok "Merged .env: $added new key(s) added, $skipped existing key(s) preserved."
}

# -----------------------------------------------------------------------------
# Reinstall confirmation
# -----------------------------------------------------------------------------
confirm_reinstall() {
    if [[ -d "$PROJECT_DIR" ]]; then
        log_warn "$PROJECT_DIR already exists."
        if [[ -t 0 ]]; then
            if ! ask_yes_no "Reinstall / overwrite the existing installation?"; then
                die "Aborted by user. Re-run without 'install' to manage the existing install."
            fi
        else
            die "$PROJECT_DIR already exists and stdin is not a TTY — refusing to overwrite."
        fi
    fi
}

# -----------------------------------------------------------------------------
# Container version reading
# -----------------------------------------------------------------------------
container_is_running() {
    silent docker ps --format '{{.Names}}' | grep -qx "$SERVICE_NAME"
}

read_installed_version() {
    if ! container_is_running; then
        return 1
    fi
    docker exec "$SERVICE_NAME" cat "$CONTAINER_VERSION_FILE" 2>/dev/null | tr -d '[:space:]'
}

# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------
cmd_install() {
    log_section "Install"
    require_root "$@"

    confirm_reinstall
    ensure_docker
    ensure_layout
    download_deployment_files
    generate_env_file
    install_cli

    if [[ -z "$(ls -A "$VOICES_DIR" 2>/dev/null)" ]]; then
        log_warn "$VOICES_DIR is empty. Drop at least one .ogg file (e.g. limited.ogg) before /send-voice will work."
    fi

    if [[ -f "$COMPOSE_FILE" ]]; then
        log_info "Pulling image and starting services…"
        compose pull
        compose up -d
        cmd_status
    else
        log_warn "Skipping 'docker compose up' — no compose file present at $COMPOSE_FILE."
    fi
}

cmd_install_cli() {
    require_root "$@"
    install_cli
}

cmd_up() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE — run '$SCRIPT_NAME install' first."
    log_info "Starting services…"
    compose up -d "$@"
    cmd_status
}

cmd_down() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE."
    log_info "Stopping services…"
    compose down "$@"
    log_ok "Stopped."
}

cmd_restart() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE."
    log_info "Restarting…"
    compose restart "$@"
    cmd_status
}

cmd_logs() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE."
    compose logs -f "$@"
}

cmd_update() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE — run '$SCRIPT_NAME install' first."

    log_section "Update"

    # Step 1: read installed version from the running container.
    local installed
    if ! installed=$(read_installed_version); then
        die "Container $SERVICE_NAME is not running. Start it with '$SCRIPT_NAME up' first."
    fi
    log_info "Installed version (from container): $installed"

    # Step 2: read the latest VERSION from the GitHub repo.
    local latest
    latest=$(curl -fsSL "${RAW_BASE}/VERSION" | tr -d '[:space:]') \
        || die "Failed to fetch latest VERSION from $RAW_BASE/VERSION."
    log_info "Latest version (from $GIT_REPO): $latest"

    # Step 3: compare.
    if [[ "$installed" == "$latest" ]]; then
        printf '%sYou already have the latest version: %s%s\n' "$C_GREEN" "$installed" "$C_RESET"
        exit 0
    fi

    if ! semver_lt "$installed" "$latest"; then
        # Installed is actually newer than upstream (e.g. dev build) — warn but
        # still let the user proceed if they want.
        log_warn "Installed version ($installed) is newer than upstream ($latest)."
    fi

    printf '%sA new version is available: %s%s\n' "$C_CYAN" "$latest" "$C_RESET"
    local reply
    while true; do
        read -r -p "Do you want to install it? (yes/no): " reply
        case "${reply,,}" in
            yes|y) break ;;
            no|n|"") die "Update cancelled by user." ;;
            *) printf "Please answer yes or no.\n" ;;
        esac
    done

    # Download latest deployment files.
    download_deployment_files

    # Merge .env against the new .env.example (preserves user settings,
    # prompts for any newly introduced keys).
    merge_env_file

    # Redeploy: stop, remove the old image to force a fresh pull, pull, up.
    log_section "Redeploying"
    log_info "Stopping the running container…"
    compose down

    if docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
        log_info "Removing existing image $DOCKER_IMAGE to force a fresh pull…"
        as_root docker rmi "$DOCKER_IMAGE" >/dev/null 2>&1 || \
            log_warn "Could not remove $DOCKER_IMAGE (continuing anyway)."
    fi

    log_info "Pulling $DOCKER_IMAGE…"
    compose pull

    log_info "Starting the new container…"
    compose up -d
    cmd_status
}

cmd_edit() {
    local target="${1:-$COMPOSE_FILE}"
    [[ -f "$target" ]] || die "No such file: $target"
    if command -v nano >/dev/null 2>&1; then
        as_root nano "$target"
    elif command -v vim >/dev/null 2>&1; then
        as_root vim "$target"
    elif command -v vi >/dev/null 2>&1; then
        as_root vi "$target"
    else
        die "No editor found. Install nano or vim, or set \$EDITOR and re-run."
    fi
}

cmd_edit_env() { cmd_edit "$ENV_FILE"; }

cmd_version() {
    if container_is_running; then
        local v
        v=$(read_installed_version)
        if [[ -n "$v" ]]; then
            printf '%s %s (container)\n' "$SCRIPT_NAME" "$v"
            return
        fi
    fi
    printf '%s %s (no running container)\n' "$SCRIPT_NAME" "$SCRIPT_VERSION"
}

cmd_status() {
    log_section "Status"
    if ! command -v docker >/dev/null 2>&1; then
        log_err "docker: NOT INSTALLED"
        return 0
    fi
    if silent docker compose version >/dev/null; then
        printf '  %s%-12s%s %s\n' "$C_BOLD" "compose"     "$C_RESET" "ok"
    else
        printf '  %s%-12s%s %s\n' "$C_BOLD" "compose"     "$C_RESET" "MISSING"
    fi
    if silent docker info >/dev/null; then
        printf '  %s%-12s%s %s\n' "$C_BOLD" "daemon"      "$C_RESET" "ok"
    else
        printf '  %s%-12s%s %s\n' "$C_BOLD" "daemon"      "$C_RESET" "unreachable"
        return 0
    fi

    # Image
    printf '  %s%-12s%s %s\n' "$C_BOLD" "image"       "$C_RESET" "$DOCKER_IMAGE"

    # Layout
    printf '  %s%-12s%s %s\n' "$C_BOLD" "project"     "$C_RESET" "$PROJECT_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "data"        "$C_RESET" "$DATA_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "voices"      "$C_RESET" "$VOICES_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "env file"    "$C_RESET" "$([ -f "$ENV_FILE" ] && echo present || echo MISSING)"

    # Container
    if container_is_running; then
        local state uptime health port version
        state=$(docker inspect -f '{{.State.Status}}' "$SERVICE_NAME" 2>/dev/null || echo "?")
        uptime=$(docker inspect -f '{{.State.StartedAt}}' "$SERVICE_NAME" 2>/dev/null || echo "?")
        health=$(docker inspect -f '{{.State.Health.Status}}' "$SERVICE_NAME" 2>/dev/null || echo "none")
        port=$(silent docker port "$SERVICE_NAME" 8000 | head -n1)
        [[ -z "$port" ]] && port="?"
        version=$(read_installed_version)
        [[ -z "$version" ]] && version="?"

        printf '  %s%-12s%s %s\n' "$C_BOLD" "container"   "$C_RESET" "${C_GREEN}running${C_RESET}"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "version"     "$C_RESET" "$version"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "state"       "$C_RESET" "$state"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "started"     "$C_RESET" "$uptime"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "health"      "$C_RESET" "$health"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "port"        "$C_RESET" "$port"
    else
        printf '  %s%-12s%s %s\n' "$C_BOLD" "container"   "$C_RESET" "${C_RED}stopped${C_RESET}"
    fi

    # HTTP health (best-effort)
    local health_url="http://localhost:${HOST_PORT:-8000}/health"
    printf '  %s%-12s%s ' "$C_BOLD" "http" "$C_RESET"
    if command -v curl >/dev/null 2>&1; then
        if silent curl -fsS --max-time 3 "$health_url" >/dev/null; then
            printf '%sok%s (%s)\n' "$C_GREEN" "$C_RESET" "$health_url"
        else
            printf '%sunreachable%s (%s)\n' "$C_RED" "$C_RESET" "$health_url"
        fi
    else
        printf '%sskipped%s (install curl)\n' "$C_DIM" "$C_RESET"
    fi
}

# -----------------------------------------------------------------------------
# Interactive menu
# -----------------------------------------------------------------------------
menu_banner() {
    printf '%s+-------------------------------------+%s\n' "$C_BOLD" "$C_RESET"
    printf '%s|   DN Notification Manager v%-7s |%s\n' "$C_BOLD$C_CYAN" "$SCRIPT_VERSION" "$C_RESET"
    printf '%s+-------------------------------------+%s\n' "$C_BOLD" "$C_RESET"
}

menu_loop() {
    while true; do
        menu_banner
        printf '  %s1)%s Install\n'                       "$C_BOLD" "$C_RESET"
        printf '  %s2)%s Up\n'                           "$C_BOLD" "$C_RESET"
        printf '  %s3)%s Down\n'                         "$C_BOLD" "$C_RESET"
        printf '  %s4)%s Restart\n'                      "$C_BOLD" "$C_RESET"
        printf '  %s5)%s Update (check + merge + redeploy)\n' "$C_BOLD" "$C_RESET"
        printf '  %s6)%s Logs (follow)\n'                "$C_BOLD" "$C_RESET"
        printf '  %s7)%s Edit docker-compose\n'          "$C_BOLD" "$C_RESET"
        printf '  %s8)%s Edit .env\n'                    "$C_BOLD" "$C_RESET"
        printf '  %s9)%s Status\n'                       "$C_BOLD" "$C_RESET"
        printf '  %s0)%s Exit\n'                         "$C_BOLD" "$C_RESET"
        printf '\n'
        local choice
        read -r -p "Select [0-9]: " choice
        case "$choice" in
            1) cmd_install ;;
            2) cmd_up ;;
            3) cmd_down ;;
            4) cmd_restart ;;
            5) cmd_update ;;
            6) cmd_logs; printf '\n' ;;
            7) cmd_edit "$COMPOSE_FILE" ;;
            8) cmd_edit "$ENV_FILE" ;;
            9) cmd_status ;;
            0) printf 'Bye.\n'; return 0 ;;
            *) log_warn "Invalid choice: $choice" ;;
        esac
        printf '\n%sPress Enter to return to the menu...%s' "$C_DIM" "$C_RESET"
        read -r _
    done
}

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------
usage() {
    cat <<EOF
$SCRIPT_NAME v$SCRIPT_VERSION - DN Notification manager

Usage:
  $SCRIPT_NAME                 Interactive menu
  $SCRIPT_NAME <command>       Run a command

Commands:
  install        One-time install: dirs, Docker, download files, .env, CLI, up
  install-cli    Install this script to $INSTALL_BIN_PATH (idempotent)
  up             docker compose up -d
  down           docker compose down
  restart        docker compose restart
  logs           docker compose logs -f
  update         Check installed version, merge .env, redeploy with new image
  edit           Open docker-compose.yaml in an editor
  edit-env       Open .env in an editor
  version        Print installed version (read from the running container)
  status         Show docker / container / health status
  help           Show this help

Source repo (hardcoded):
  $GIT_REPO  (branch: $GIT_BRANCH)

Docker image (hardcoded):
  $DOCKER_IMAGE
EOF
}

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
main() {
    if [[ $# -eq 0 ]]; then
        menu_loop
        exit 0
    fi

    case "$1" in
        install)        shift; cmd_install "$@" ;;
        install-cli)    shift; cmd_install_cli "$@" ;;
        up)             shift; cmd_up "$@" ;;
        down)           shift; cmd_down "$@" ;;
        restart)        shift; cmd_restart "$@" ;;
        logs)           shift; cmd_logs "$@" ;;
        update)         shift; cmd_update "$@" ;;
        edit)           shift; cmd_edit "$@" ;;
        edit-env)       shift; cmd_edit_env "$@" ;;
        version)        shift; cmd_version "$@" ;;
        status)         shift; cmd_status "$@" ;;
        help|-h|--help) usage ;;
        *) die "Unknown command: $1 (try '$SCRIPT_NAME help')" ;;
    esac
}

main "$@"
