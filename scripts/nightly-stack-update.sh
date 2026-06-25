#!/usr/bin/env bash
# Nightly stack maintenance for hermes-webui-stack + Plex
# Hermes stack path is autonomous rollout: host-side v2 apply performs
# source reconciliation, preflight, backups, candidate builds, selected service
# recreates, post-rollout probes, and rollback if verification fails. Tailscale
# and Hindsight remain excluded from normal automated recreates. Plex handling is separate.
# Logs to ~/docker-compose/hermes-webui-stack/logs/nightly-stack-update.log
# Prevents duplicate runs via lock file.

set -euo pipefail

LOG_DIR="${HOME}/docker-compose/hermes-webui-stack/logs"
LOCK_FILE="${LOG_DIR}/nightly-stack-update.lock"
LOG_FILE="${LOG_DIR}/nightly-stack-update.log"

# Lock to prevent overlapping runs
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
    echo "$(date -Iseconds) [SKIP] Another instance is already running (lock held). Exiting." >> "${LOG_FILE}"
    exit 0
fi

log() {
    echo "$(date -Iseconds) [NIGHTLY] $1" | tee -a "${LOG_FILE}"
}

log_start() {
    mkdir -p "${LOG_DIR}"
    log "=== Nightly stack update/restart START ==="
}

log_end() {
    log "=== Nightly stack update/restart END (exit=$1) ==="
}

trap 'log_end $?' EXIT

log_start

# --- hermes-webui-stack autonomous host-side upgrade ---
STACK_SCRIPT="${HOME}/docker-compose/hermes-webui-stack/scripts/stack_upgrade_v2.py"
STACK_DIR="${HOME}/docker-compose/hermes-webui-stack"
AUTONOMOUS_HERMES_UPGRADE="${HOME}/docker-compose/hermes-webui-stack/scripts/hermes-autonomous-upgrade.sh"

if [[ -x "${AUTONOMOUS_HERMES_UPGRADE}" ]]; then
    log "Running hermes-webui-stack autonomous upgrade (host-side apply; excludes tailscale/hindsight)..."
    if bash "${AUTONOMOUS_HERMES_UPGRADE}" >> "${LOG_FILE}" 2>&1; then
        log "hermes-webui-stack autonomous upgrade: OK"
    else
        log "hermes-webui-stack autonomous upgrade: BLOCKED/FAILED (check logs/hermes-autonomous-upgrade.log and state/upgrade-v2/last-autonomous-upgrade.json)"
    fi
elif [[ -x "${STACK_SCRIPT}" ]]; then
    log "WARN: autonomous wrapper missing; falling back to source reconcile + preflight only"
    if python3 "${STACK_SCRIPT}" reconcile-source --fetch --promote --json >> "${LOG_FILE}" 2>&1; then
        python3 "${STACK_SCRIPT}" preflight --fetch --json >> "${LOG_FILE}" 2>&1 || true
    fi
else
    log "WARN: stack_upgrade_v2.py not found at ${STACK_SCRIPT}, skipping hermes-webui-stack"
fi

# --- Plex update and restart ---
PLEX_DIR="${HOME}/docker-compose/plex"
if [[ -d "${PLEX_DIR}" ]]; then
    log "Pulling and restarting Plex container..."
    # Pull latest image (stays on plexpass tag)
    if docker compose --project-directory "${PLEX_DIR}" pull plex >> "${LOG_FILE}" 2>&1; then
        log "Plex image pulled OK"
    else
        log "Plex image pull failed or already up-to-date"
    fi

    # Recreate only the plex container (preserves config/transcode/data volumes)
    if docker compose --project-directory "${PLEX_DIR}" up -d plex >> "${LOG_FILE}" 2>&1; then
        # Wait for Plex to become healthy (up to 120s)
        log "Plex container recreated, waiting for healthy status..."
        for i in $(seq 1 24); do
            sleep 5
            STATUS=$(docker inspect plex --format='{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
            if [[ "${STATUS}" == "healthy" ]]; then
                log "Plex is healthy after restart."
                break
            fi
            log "  Plex status: ${STATUS} (wait ${i}/24)"
        done
    else
        log "ERROR: Plex container recreate failed"
    fi
else
    log "WARN: Plex compose dir not found at ${PLEX_DIR}, skipping Plex"
fi

log "Nightly update/restart complete."
