#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
PURGE=false

usage() {
    cat <<'EOF'
Usage: uninstall.sh [options]

Options:
  --dry-run   Print every step and command without changing the system
  --purge     Also remove /var/lib/agentic, /etc/agentic, ledgers (after chattr -a), workspaces, spool, and backups
  -h, --help  Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --purge)
            PURGE=true
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

PREFIX="${AGENTIC_ROOT_PREFIX:-}"
if [[ "$PREFIX" != "/" ]]; then
    PREFIX="${PREFIX%/}"
fi

step() {
    echo "==> $*"
}

run_cmd() {
    echo "+ $*"
    if ! "$DRY_RUN"; then
        "$@"
    fi
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if "$DRY_RUN"; then
        echo "notice: running as non-root user (continuing under --dry-run)"
    else
        echo "error: uninstall.sh must be run as root" >&2
        exit 1
    fi
fi

# 1. Stop, disable, and remove systemd units and timers
step "units/timers"
UNITS=(
    agentic-inbox.path
    agentic-inbox.timer
    agentic-daily.timer
    agentic-health.timer
    agentic-telegram.service
)
echo "+ systemctl disable --now ${UNITS[*]}"
if ! "$DRY_RUN"; then
    systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
fi

UNIT_FILES=(
    "${PREFIX}/etc/systemd/system/agentic@.service"
    "${PREFIX}/etc/systemd/system/agentic-failed@.service"
    "${PREFIX}/etc/systemd/system/agentic-inbox.path"
    "${PREFIX}/etc/systemd/system/agentic-inbox.service"
    "${PREFIX}/etc/systemd/system/agentic-inbox.timer"
    "${PREFIX}/etc/systemd/system/agentic-daily.service"
    "${PREFIX}/etc/systemd/system/agentic-daily.timer"
    "${PREFIX}/etc/systemd/system/agentic-health.service"
    "${PREFIX}/etc/systemd/system/agentic-health.timer"
    "${PREFIX}/etc/systemd/system/agentic-telegram.service"
)
run_cmd rm -f "${UNIT_FILES[@]}"
run_cmd systemctl daemon-reload

# 2. Remove launchers
step "launchers"
run_cmd rm -f \
    "${PREFIX}/usr/local/sbin/agentic" \
    "${PREFIX}/usr/local/sbin/mission" \
    "${PREFIX}/usr/local/sbin/agentic-guidance-sync" \
    "${PREFIX}/usr/local/sbin/agentic-guidance-check"

# 3. Remove package
step "package"
run_cmd rm -rf "${PREFIX}/opt/agentic"

# 4. State, config, ledgers, spool, and backups
step "state"
if "$PURGE"; then
    echo "+ chattr -a ${PREFIX}/var/lib/agentic/missions/*/ledger.jsonl"
    if ! "$DRY_RUN"; then
        shopt -s nullglob
        for ledger in "${PREFIX}/var/lib/agentic/missions/"*/ledger.jsonl; do
            chattr -a "$ledger" 2>/dev/null || true
        done
        shopt -u nullglob
    fi
    run_cmd rm -rf \
        "${PREFIX}/var/lib/agentic" \
        "${PREFIX}/etc/agentic" \
        "${PREFIX}/var/lib/agwork/missions" \
        "${PREFIX}/var/spool/agentic" \
        "${PREFIX}/var/backups/agentic"
else
    echo "Keeping state (/var/lib/agentic), configuration (/etc/agentic), and ledgers (pass --purge to remove)."
fi

echo "Note: system accounts (agwork, agplan, agbot, agent) and group procview are kept."
