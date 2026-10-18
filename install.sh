#!/usr/bin/env bash
set -euo pipefail

OMP_BIN=""
PYTHON_BIN="/usr/bin/python3.12"
TIMEZONE="UTC"
LANGUAGE="en"
TELEGRAM=false
HIDEPID=true
AGENT_SUDO=false
DRY_RUN=false

usage() {
    cat <<'EOF'
Usage: install.sh --omp PATH [options]

Options:
  --omp PATH          Path to the omp executable to pin at /opt/omp/omp (required)
  --python PATH       Python 3.12 executable (default: /usr/bin/python3.12)
  --timezone TZ       Display IANA time zone (default: UTC)
  --language en|ko    Display language (default: en)
  --telegram          Enable and install the Telegram bot unit (default: off)
  --no-telegram       Disable the Telegram bot unit
  --hidepid           Mount /proc with hidepid=2 and group procview (default: on)
  --no-hidepid        Skip /proc hidepid setup
  --agent-sudo        Install sudoers rule for the interactive agent (default: off)
  --no-agent-sudo     Do not install sudoers rule for the interactive agent
  --dry-run           Print every step and command without changing the system
  -h, --help          Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --omp)
            if [[ $# -lt 2 ]]; then
                echo "error: --omp requires a PATH argument" >&2
                exit 2
            fi
            OMP_BIN="$2"
            shift 2
            ;;
        --omp=*)
            OMP_BIN="${1#*=}"
            shift
            ;;
        --python)
            if [[ $# -lt 2 ]]; then
                echo "error: --python requires a PATH argument" >&2
                exit 2
            fi
            PYTHON_BIN="$2"
            shift 2
            ;;
        --python=*)
            PYTHON_BIN="${1#*=}"
            shift
            ;;
        --timezone)
            if [[ $# -lt 2 ]]; then
                echo "error: --timezone requires a TZ argument" >&2
                exit 2
            fi
            TIMEZONE="$2"
            shift 2
            ;;
        --timezone=*)
            TIMEZONE="${1#*=}"
            shift
            ;;
        --language)
            if [[ $# -lt 2 ]]; then
                echo "error: --language requires en or ko" >&2
                exit 2
            fi
            LANGUAGE="$2"
            shift 2
            ;;
        --language=*)
            LANGUAGE="${1#*=}"
            shift
            ;;
        --telegram)
            TELEGRAM=true
            shift
            ;;
        --no-telegram)
            TELEGRAM=false
            shift
            ;;
        --hidepid)
            HIDEPID=true
            shift
            ;;
        --no-hidepid)
            HIDEPID=false
            shift
            ;;
        --agent-sudo)
            AGENT_SUDO=true
            shift
            ;;
        --no-agent-sudo)
            AGENT_SUDO=false
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$OMP_BIN" ]]; then
    echo "error: --omp PATH is required" >&2
    exit 2
fi

if [[ "$LANGUAGE" != "en" && "$LANGUAGE" != "ko" ]]; then
    echo "error: --language must be 'en' or 'ko' (got '$LANGUAGE')" >&2
    exit 2
fi

if [[ -z "$TIMEZONE" ]]; then
    echo "error: --timezone must not be empty" >&2
    exit 2
fi

PREFIX="${AGENTIC_ROOT_PREFIX:-}"
if [[ "$PREFIX" != "/" ]]; then
    PREFIX="${PREFIX%/}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERACTIVE_USER="${AGENTIC_INTERACTIVE_USER:-agent}"

step() {
    echo "==> $*"
}

run_cmd() {
    echo "+ $*"
    if ! "$DRY_RUN"; then
        "$@"
    fi
}

GETENT_BIN="$(command -v getent || true)"
is_host_getent() {
    [[ "$GETENT_BIN" == "/usr/bin/getent" || "$GETENT_BIN" == "/bin/getent" ]]
}

account_exists() {
    local acct="$1"
    local home_rel="$2"
    if [[ -n "$PREFIX" && ! -f "${PREFIX}/etc/passwd" ]] && is_host_getent; then
        [[ -d "${PREFIX}${home_rel}" ]]
    else
        getent passwd "$acct" >/dev/null 2>&1
    fi
}

group_exists() {
    local grp="$1"
    if [[ -n "$PREFIX" && ! -f "${PREFIX}/etc/group" ]] && is_host_getent; then
        return 1
    else
        getent group "$grp" >/dev/null 2>&1
    fi
}

# 1. Prerequisites
step "prerequisites"
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if "$DRY_RUN"; then
        echo "notice: running as non-root user (continuing under --dry-run)"
    else
        echo "error: install.sh must be run as root" >&2
        exit 1
    fi
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found; systemd is required" >&2
    exit 1
fi
if ! "$DRY_RUN" && [[ -z "$PREFIX" && ! -d /run/systemd/system ]]; then
    echo "error: systemd is not active (/run/systemd/system not found)" >&2
    exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "error: Python 3.12 executable not found or not executable: $PYTHON_BIN" >&2
    exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1; then
    echo "error: $PYTHON_BIN is not Python 3.12" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "error: git not found" >&2
    exit 1
fi

if [[ ! -f "$OMP_BIN" || ! -x "$OMP_BIN" ]]; then
    echo "error: --omp binary not found or not executable: $OMP_BIN" >&2
    exit 1
fi

# 2. Accounts
step "accounts"
if ! getent passwd "$INTERACTIVE_USER" >/dev/null 2>&1; then
    echo "error: interactive user '$INTERACTIVE_USER' does not exist" >&2
    exit 1
fi
INTERACTIVE_HOME="$(getent passwd "$INTERACTIVE_USER" | cut -d: -f6 || true)"
if [[ -z "$INTERACTIVE_HOME" ]]; then
    INTERACTIVE_HOME="/home/${INTERACTIVE_USER}"
fi

for acct in agwork agplan agbot; do
    acct_home="${PREFIX}/var/lib/${acct}"
    if ! account_exists "$acct" "/var/lib/${acct}"; then
        run_cmd useradd --system --create-home --home-dir "$acct_home" --shell /usr/sbin/nologin "$acct"
    fi
    run_cmd install -d -m 0700 -o "$acct" -g "$acct" "$acct_home"
    run_cmd chmod 0700 "$acct_home"
done

# 3. Folders
step "folders"
run_cmd install -d -m 0700 -o root -g root "${PREFIX}/var/lib/agentic"
run_cmd install -d -m 0700 -o agwork -g agwork "${PREFIX}/var/lib/agwork/missions"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/var/spool/agentic"
run_cmd install -d -m 0700 -o agbot -g agbot "${PREFIX}/var/spool/agentic/inbox"
run_cmd install -d -m 0700 -o agbot -g agbot "${PREFIX}/var/spool/agentic/outbox"
run_cmd install -d -m 0700 -o root -g root "${PREFIX}/var/backups/agentic"

# 4. Package
step "package"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/opt/agentic"
if [[ -d "$REPO_ROOT/.git" ]] && git -C "$REPO_ROOT" rev-parse --verify HEAD >/dev/null 2>&1; then
    REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    echo "+ git -C $REPO_ROOT archive HEAD | tar -x -C ${PREFIX}/opt/agentic"
    if ! "$DRY_RUN"; then
        git -C "$REPO_ROOT" archive HEAD | tar -x -C "${PREFIX}/opt/agentic"
    fi
    # guidance/ is copied in by the operator and may not be committed yet; install the working copy then.
    if [[ -d "$REPO_ROOT/guidance" ]] && [[ -z "$(git -C "$REPO_ROOT" ls-files -- guidance | head -n 1)" ]]; then
        run_cmd cp -a "$REPO_ROOT/guidance" "${PREFIX}/opt/agentic/"
    fi
else
    REVISION="release-1.0.0"
    run_cmd cp -a "$REPO_ROOT/agentic" "${PREFIX}/opt/agentic/"
    if [[ -d "$REPO_ROOT/guidance" ]]; then
        run_cmd cp -a "$REPO_ROOT/guidance" "${PREFIX}/opt/agentic/"
    fi
    if [[ -d "$REPO_ROOT/deploy" ]]; then
        run_cmd cp -a "$REPO_ROOT/deploy" "${PREFIX}/opt/agentic/"
    fi
    if [[ -d "$REPO_ROOT/docs" ]]; then
        run_cmd cp -a "$REPO_ROOT/docs" "${PREFIX}/opt/agentic/"
    fi
fi
echo "+ write ${PREFIX}/opt/agentic/REVISION (${REVISION})"
if ! "$DRY_RUN"; then
    printf '%s\n' "$REVISION" > "${PREFIX}/opt/agentic/REVISION"
    chmod 0644 "${PREFIX}/opt/agentic/REVISION"
fi
run_cmd chown -R root:root "${PREFIX}/opt/agentic"
run_cmd chmod -R go-w "${PREFIX}/opt/agentic"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/opt/omp"
run_cmd install -m 0755 -o root -g root "$OMP_BIN" "${PREFIX}/opt/omp/omp"

# 5. Launchers
step "launchers"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/usr/local/sbin"
for launcher in agentic mission agentic-guidance-sync agentic-guidance-check; do
    run_cmd install -m 0755 -o root -g root "$REPO_ROOT/deploy/bin/${launcher}" "${PREFIX}/usr/local/sbin/${launcher}"
done

# 6. Units and timers
step "units/timers"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/etc/systemd/system"
UNITS_TO_INSTALL=(
    agentic@.service
    agentic-failed@.service
    agentic-inbox.path
    agentic-inbox.service
    agentic-inbox.timer
    agentic-daily.service
    agentic-daily.timer
    agentic-health.service
    agentic-health.timer
)
if "$TELEGRAM"; then
    UNITS_TO_INSTALL+=(agentic-telegram.service)
fi
for unit in "${UNITS_TO_INSTALL[@]}"; do
    run_cmd install -m 0644 -o root -g root "$REPO_ROOT/deploy/systemd/${unit}" "${PREFIX}/etc/systemd/system/${unit}"
done
run_cmd systemctl daemon-reload
UNITS_TO_ENABLE=(
    agentic-inbox.path
    agentic-inbox.timer
    agentic-daily.timer
    agentic-health.timer
)
if "$TELEGRAM"; then
    UNITS_TO_ENABLE+=(agentic-telegram.service)
fi
run_cmd systemctl enable --now "${UNITS_TO_ENABLE[@]}"

# 7. Configuration
step "config"
run_cmd install -d -m 0755 -o root -g root "${PREFIX}/etc/agentic"
TOML_PATH="${PREFIX}/etc/agentic/agentic.toml"
TOML_CONTENT="$(cat <<EOF
[accounts]
interactive_user = "${INTERACTIVE_USER}"
worker_user = "agwork"
planner_user = "agplan"
bot_user = "agbot"

[paths]
state = "/var/lib/agentic"
workspaces = "/var/lib/agwork/missions"
spool = "/var/spool/agentic"
worker_omp = "/opt/omp/omp"
planner_omp = "/opt/omp/omp"
worker_overlay = "/etc/agentic/omp-overlay.yml"
guidance_sync = "/usr/local/sbin/agentic-guidance-sync"
guidance_check = "/usr/local/sbin/agentic-guidance-check"

[display]
timezone = "${TIMEZONE}"
language = "${LANGUAGE}"

[telegram]
enabled = ${TELEGRAM}
EOF
)"
if [[ ! -f "$TOML_PATH" ]]; then
    echo "+ install -m 0644 -o root -g root /dev/stdin ${TOML_PATH}"
    printf '%s\n' "$TOML_CONTENT"
    if ! "$DRY_RUN"; then
        printf '%s\n' "$TOML_CONTENT" > "$TOML_PATH"
        chown root:root "$TOML_PATH"
        chmod 0644 "$TOML_PATH"
    fi
fi

OVERLAY_PATH="${PREFIX}/etc/agentic/omp-overlay.yml"
OVERLAY_CONTENT="$(cat <<'EOF'
retry:
  modelFallback: false
advisor:
  enabled: false
EOF
)"
if [[ ! -f "$OVERLAY_PATH" ]]; then
    echo "+ install -m 0644 -o root -g root /dev/stdin ${OVERLAY_PATH}"
    printf '%s\n' "$OVERLAY_CONTENT"
    if ! "$DRY_RUN"; then
        printf '%s\n' "$OVERLAY_CONTENT" > "$OVERLAY_PATH"
        chown root:root "$OVERLAY_PATH"
        chmod 0644 "$OVERLAY_PATH"
    fi
fi

# 8. /proc hidepid
step "hidepid"
FSTAB_LINE="proc /proc proc defaults,hidepid=2,gid=procview 0 0"
if "$HIDEPID"; then
    if ! group_exists procview; then
        run_cmd groupadd --system procview
    fi
    FSTAB_PATH="${PREFIX}/etc/fstab"
    if [[ ! -f "$FSTAB_PATH" ]] || ! grep -qF "$FSTAB_LINE" "$FSTAB_PATH"; then
        echo "+ append ${FSTAB_PATH}: ${FSTAB_LINE}"
        if ! "$DRY_RUN"; then
            mkdir -p "$(dirname "$FSTAB_PATH")"
            printf '%s\n' "$FSTAB_LINE" >> "$FSTAB_PATH"
        fi
    fi
    run_cmd mount -o remount /proc
fi

# 9. Interactive agent sudo
step "agent sudo"
if "$AGENT_SUDO"; then
    SUDOERS_SRC="$REPO_ROOT/deploy/sudoers/agentic-agent.example"
    SUDOERS_DST="${PREFIX}/etc/sudoers.d/agentic-agent"
    run_cmd visudo -cf "$SUDOERS_SRC"
    run_cmd install -d -m 0750 -o root -g root "${PREFIX}/etc/sudoers.d"
    run_cmd install -m 0440 -o root -g root "$SUDOERS_SRC" "$SUDOERS_DST"
fi

# 10. Guidance sync and lock
step "guidance"
if [[ -n "$PREFIX" && "$INTERACTIVE_HOME" == "$PREFIX"/* ]]; then
    GUIDANCE_DST="${INTERACTIVE_HOME}/.omp/agent"
else
    GUIDANCE_DST="${PREFIX}${INTERACTIVE_HOME}/.omp/agent"
fi
run_cmd "${PREFIX}/usr/local/sbin/agentic-guidance-sync"
run_cmd chattr +i "${GUIDANCE_DST}/RULES.md" "${GUIDANCE_DST}/AGENTS.md" "${GUIDANCE_DST}/WATCHDOG.md"

# 11. Final message
step "next steps"
echo "Manual step required: configure omp-auth-gateway with per-account client tokens for agwork and agplan, and log in to your model providers."
echo "See docs/OPERATIONS.md for auth gateway setup, Telegram bot setup, daily jobs, backups, upgrades, and migration."
