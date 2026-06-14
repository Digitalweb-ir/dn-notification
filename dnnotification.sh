#!/usr/bin/env bash
# =============================================================================
#  dnnotification.sh — DN Notification manager CLI
# =============================================================================
#  Manages the dockerized DN Notification (Telegram MTProto automation) service.
#
#  Host layout (defaults — override via env vars before running):
#    PROJECT_DIR = /opt/dn-notification
#    DATA_DIR    = /var/lib/dn-notification    <- ALL persistent data lives here
#      ├── session/   Telegram .session file (account credential)
#      ├── logs/      Application logs
#      └── voices/    Voice templates (.ogg files)
#
#  Project files (docker-compose.yaml, .env) live under PROJECT_DIR.
#
#  Usage:
#    dnnotification                 -> interactive menu
#    dnnotification <command>       -> run a command
#    dnnotification help            -> list commands
#
#  Git repo used to fetch docker-compose.yaml / .env.example during install.
#  Edit GIT_REPO at the top of this script if you forked the project.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

# -----------------------------------------------------------------------------
# Config — change GIT_REPO / DOCKER_IMAGE here if you forked the project
# -----------------------------------------------------------------------------
GIT_REPO="https://github.com/erfan/dn-notification"
GIT_BRANCH="${GIT_BRANCH:-main}"
RAW_BASE="https://raw.githubusercontent.com/erfan/dn-notification/${GIT_BRANCH}"

PROJECT_DIR="${PROJECT_DIR:-/opt/dn-notification}"
DATA_DIR="${DATA_DIR:-/var/lib/dn-notification}"
VOICES_DIR="${VOICES_DIR:-${DATA_DIR}/voices}"
SESSION_DIR="${SESSION_DIR:-${DATA_DIR}/session}"
LOGS_DIR="${LOGS_DIR:-${DATA_DIR}/logs}"
COMPOSE_FILE="${COMPOSE_FILE:-${PROJECT_DIR}/docker-compose.yaml}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
ENV_EXAMPLE="${ENV_EXAMPLE:-${PROJECT_DIR}/.env.example}"

# Source script (this file) and the install target.
SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_PATH="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)/$(basename "$SCRIPT_PATH")"
INSTALL_BIN_DIR="${INSTALL_BIN_DIR:-/usr/local/bin}"
INSTALL_BIN_NAME="${INSTALL_BIN_NAME:-dnnotification}"   # no extension
INSTALL_BIN_PATH="${INSTALL_BIN_DIR}/${INSTALL_BIN_NAME}"

HEALTH_URL="${HEALTH_URL:-http://localhost:${HOST_PORT:-8000}/health}"
SERVICE_NAME="${SERVICE_NAME:-dn-notification}"
SCRIPT_NAME="dnnotification"   # logical name regardless of how this file is invoked
SCRIPT_VERSION="2.0.0"

# -----------------------------------------------------------------------------
# Colors (auto-disabled if stdout isn't a TTY or NO_COLOR is set).
# -----------------------------------------------------------------------------
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
fi

# -----------------------------------------------------------------------------
# Logging helpers
# -----------------------------------------------------------------------------
log_info()    { printf '%s[i]%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
log_ok()      { printf '%s[OK]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
log_warn()    { printf '%s[!]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
log_err()     { printf '%s[X]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }
log_section() { printf '\n%s== %s ==%s\n' "$C_BOLD$C_CYAN" "$*" "$C_RESET"; }

die() { log_err "$*"; exit 1; }
silent() { "$@" 2>/dev/null || true; }
# Abort on signals, but don't fire on normal command failures.
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

# -----------------------------------------------------------------------------
# Privilege gate — `install` and `install-cli` must run as root.
# -----------------------------------------------------------------------------
require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "This command must be run as root (try: sudo $SCRIPT_NAME $*)."
    fi
}

# -----------------------------------------------------------------------------
# Dependency detection
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
        cat <<EOF
  Install the v2 plugin:
    Debian/Ubuntu : sudo apt-get install -y docker-compose-plugin
    macOS         : bundled with Docker Desktop
EOF
        return 1
    fi

    if ! docker info >/dev/null 2>&1; then
        log_err "Cannot communicate with the Docker daemon."
        cat <<EOF
  Either:
    - Start it:  sudo systemctl start docker
    - Add your user to the docker group:
        sudo usermod -aG docker \$USER   (then log out and back in)
EOF
        return 1
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Small utilities
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

# Prompt the user for a value, refusing blank input. Echoes the value to stdout.
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

    # Hand the bind-mount to UID 1000 (the container's svc user) so the
    # non-root process can write inside the container.
    if id -u 1000 >/dev/null 2>&1; then
        as_root chown -R 1000:1000 "$DATA_DIR"
    fi
    log_ok "Project dir: $PROJECT_DIR"
    log_ok "Data dir:    $DATA_DIR"
    log_ok "  ├─ session: $SESSION_DIR"
    log_ok "  ├─ logs:    $LOGS_DIR"
    log_ok "  └─ voices:  $VOICES_DIR"
}

# -----------------------------------------------------------------------------
# Compose helper — every lifecycle command goes through this so we can enforce
# a single working directory and consistent error handling.
# -----------------------------------------------------------------------------
compose() {
    ( cd "$PROJECT_DIR" && docker compose "$@" )
}

# -----------------------------------------------------------------------------
# Install this CLI to /usr/local/bin/dnnotification (extension stripped, +x).
# Idempotent — re-running just refreshes the file in place.
# -----------------------------------------------------------------------------
install_cli() {
    log_section "Install CLI"

    if [[ ! -f "$SCRIPT_PATH" ]]; then
        die "Could not locate this script at $SCRIPT_PATH — refusing to self-install."
    fi

    as_root install -d -m 0755 "$INSTALL_BIN_DIR"

    # `install` with explicit dest name strips the .sh extension because we
    # name the destination file as $INSTALL_BIN_NAME (no extension).
    as_root install -m 0755 "$SCRIPT_PATH" "$INSTALL_BIN_PATH"

    if [[ -x "$INSTALL_BIN_PATH" ]]; then
        log_ok "Installed: $INSTALL_BIN_PATH"
    else
        die "Install appeared to succeed but $INSTALL_BIN_PATH is not executable."
    fi

    # Confirm it's discoverable on PATH.
    if command -v "$INSTALL_BIN_NAME" >/dev/null 2>&1; then
        log_ok "On PATH: $(command -v "$INSTALL_BIN_NAME")"
    else
        log_warn "$INSTALL_BIN_DIR is not on \$PATH for this shell."
        log_warn "Add it:  export PATH=\"$INSTALL_BIN_DIR:\$PATH\""
    fi
}

# -----------------------------------------------------------------------------
# Download deployment files from GitHub
# -----------------------------------------------------------------------------
fetch_from_repo() {
    local relpath="$1" dest="$2"
    local url="${RAW_BASE}/${relpath}"
    log_info "Downloading $url"
    have_curl || die "curl is required to fetch deployment files."
    if ! curl -fsSL --retry 3 -o "$dest" "$url"; then
        die "Failed to download $relpath from $url — check GIT_REPO/GIT_BRANCH."
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
# Interactive .env generation
# -----------------------------------------------------------------------------
generate_env_file() {
    log_section "Configure environment"
    log_info "Prompts cannot be skipped — required values only. You can"
    log_info "fine-tune other settings later with: $SCRIPT_NAME edit-env"

    local tg_api_id tg_api_hash tg_phone api_key

    tg_api_id=$(prompt_required "Telegram API ID (https://my.telegram.org/apps)")
    tg_api_hash=$(prompt_required "Telegram API hash")
    tg_phone=$(prompt_required "Telegram phone (international format, e.g. +1234567890)")
    api_key=$(prompt_required "API_KEY (long random string for X-API-KEY auth)")

    if [[ ! -f "$ENV_EXAMPLE" ]]; then
        die "Missing template $ENV_EXAMPLE — re-run install or download it manually."
    fi

    # Substitute placeholders in the template. Use awk so the values are
    # written verbatim (no shell expansion of user input).
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

    # Install (or refresh) the CLI itself.
    install_cli

    # Make sure voices dir isn't empty — it's confusing for new users.
    if [[ -z "$(ls -A "$VOICES_DIR" 2>/dev/null)" ]]; then
        log_warn "$VOICES_DIR is empty. Drop at least one .ogg file (e.g. limited.ogg) before /send-voice will work."
    fi

    # Bring the stack up. Image is pulled (not built) because compose.yaml
    # references a pre-built image with no `build:` context.
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

cmd_pull() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE."
    log_info "Pulling latest image…"
    compose pull
}

cmd_update() {
    check_docker
    [[ -f "$COMPOSE_FILE" ]] || die "No compose file at $COMPOSE_FILE."
    log_info "Re-downloading deployment files from $GIT_REPO…"
    download_deployment_files
    log_info "Pulling latest image…"
    compose pull
    log_info "Restarting with new image…"
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

cmd_status() {
    log_section "Status"
    # Docker available?
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
        return 0   # report what we know, don't abort
    fi

    # Layout
    printf '  %s%-12s%s %s\n' "$C_BOLD" "project"     "$C_RESET" "$PROJECT_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "data"        "$C_RESET" "$DATA_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "voices"      "$C_RESET" "$VOICES_DIR"
    printf '  %s%-12s%s %s\n' "$C_BOLD" "env file"    "$C_RESET" "$([ -f "$ENV_FILE" ] && echo present || echo MISSING)"

    # Container
    if silent docker ps --format '{{.Names}}' | grep -qx "$SERVICE_NAME"; then
        local state uptime health port
        state=$(docker inspect -f '{{.State.Status}}' "$SERVICE_NAME" 2>/dev/null || echo "?")
        uptime=$(docker inspect -f '{{.State.StartedAt}}' "$SERVICE_NAME" 2>/dev/null || echo "?")
        health=$(docker inspect -f '{{.State.Health.Status}}' "$SERVICE_NAME" 2>/dev/null || echo "none")
        port=$(silent docker port "$SERVICE_NAME" 8000 | head -n1)
        [[ -z "$port" ]] && port="?"

        printf '  %s%-12s%s %s\n' "$C_BOLD" "container"   "$C_RESET" "${C_GREEN}running${C_RESET}"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "state"       "$C_RESET" "$state"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "started"     "$C_RESET" "$uptime"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "health"      "$C_RESET" "$health"
        printf '  %s%-12s%s %s\n' "$C_BOLD" "port"        "$C_RESET" "$port"
    else
        printf '  %s%-12s%s %s\n' "$C_BOLD" "container"   "$C_RESET" "${C_RED}stopped${C_RESET}"
    fi

    # HTTP health (best-effort)
    printf '  %s%-12s%s ' "$C_BOLD" "http" "$C_RESET"
    if command -v curl >/dev/null 2>&1; then
        if silent curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null; then
            printf '%sok%s (%s)\n' "$C_GREEN" "$C_RESET" "$HEALTH_URL"
        else
            printf '%sunreachable%s (%s)\n' "$C_RED" "$C_RESET" "$HEALTH_URL"
        fi
    else
        printf '%sskipped%s (install curl)\n' "$C_DIM" "$C_RESET"
    fi
}

# -----------------------------------------------------------------------------
# Interactive menu
# -----------------------------------------------------------------------------
menu_banner() {
    printf '%s+----------------------------------+%s\n' "$C_BOLD" "$C_RESET"
    printf '%s|   DN Notification Manager v%-7s|%s\n' "$C_BOLD$C_CYAN" "$SCRIPT_VERSION" "$C_RESET"
    printf '%s+----------------------------------+%s\n' "$C_BOLD" "$C_RESET"
}

menu_loop() {
    while true; do
        menu_banner
        printf '  %s1)%s Install\n'                "$C_BOLD" "$C_RESET"
        printf '  %s2)%s Up\n'                    "$C_BOLD" "$C_RESET"
        printf '  %s3)%s Down\n'                  "$C_BOLD" "$C_RESET"
        printf '  %s4)%s Restart\n'               "$C_BOLD" "$C_RESET"
        printf '  %s5)%s Update (re-download + pull)\n' "$C_BOLD" "$C_RESET"
        printf '  %s6)%s Logs (follow)\n'         "$C_BOLD" "$C_RESET"
        printf '  %s7)%s Edit docker-compose\n'   "$C_BOLD" "$C_RESET"
        printf '  %s8)%s Edit .env\n'             "$C_BOLD" "$C_RESET"
        printf '  %s9)%s Status\n'                "$C_BOLD" "$C_RESET"
        printf '  %s0)%s Exit\n'                  "$C_BOLD" "$C_RESET"
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
  pull           docker compose pull (refresh the image)
  update         Re-download compose/.env.example, pull image, restart
  edit           Open docker-compose.yaml in an editor
  edit-env       Open .env in an editor
  status         Show docker / container / health status
  help           Show this help
  version        Print version

Layout (override with env vars PROJECT_DIR / DATA_DIR / VOICES_DIR):
  Project : $PROJECT_DIR
  Data    : $DATA_DIR
    session : $SESSION_DIR
    logs    : $LOGS_DIR
    voices  : $VOICES_DIR

Source repo for install-time downloads (edit GIT_REPO at the top of the script):
  $GIT_REPO  (branch: $GIT_BRANCH)
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
        pull)           shift; cmd_pull "$@" ;;
        update)         shift; cmd_update "$@" ;;
        edit)           shift; cmd_edit "$@" ;;
        edit-env)       shift; cmd_edit_env "$@" ;;
        status)         shift; cmd_status "$@" ;;
        help|-h|--help) usage ;;
        version|-v|--version) printf '%s %s\n' "$SCRIPT_NAME" "$SCRIPT_VERSION" ;;
        *) die "Unknown command: $1 (try '$SCRIPT_NAME help')" ;;
    esac
}

main "$@"
