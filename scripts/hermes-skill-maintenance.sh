#!/usr/bin/env bash
# Host-side Hermes skill maintenance lane wrapper.
# Runs outside Hermes/WebUI containers and shares the autonomous-upgrade lock so
# skill maintenance cannot overlap container recreation.

set -euo pipefail

STACK_DIR="/home/jmelchiori/docker-compose/hermes-webui-stack"
LANE_SCRIPT="${STACK_DIR}/scripts/skill_maintenance_lane.py"
LOG_DIR="${STACK_DIR}/logs"
STATE_DIR="${STACK_DIR}/state/skill-maintenance"
LOCK_FILE="/tmp/hermes-webui-stack-autonomous-upgrade.lock"
MODE="${1:-report}"
shift || true

mkdir -p "${LOG_DIR}" "${STATE_DIR}"
LOG_FILE="${LOG_DIR}/hermes-skill-maintenance.log"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
LAST_JSON="${STATE_DIR}/last-run.json"

log() {
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG_FILE}"
}

json_status() {
    python3 - "$1" <<'JSON_STATUS_PY'
import json, sys
try:
    data=json.load(open(sys.argv[1]))
    print(data.get('status','unknown'))
except Exception as exc:
    print(f'unreadable:{exc}')
JSON_STATUS_PY
}

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "Another Hermes stack maintenance run is already active; exiting."
    exit 0
fi

cd "${STACK_DIR}"
log "=== Hermes skill maintenance ${MODE} START ==="
log "host=$(hostname) user=$(id -un) stack_dir=${STACK_DIR}"

run_json="${STATE_DIR}/skill-maintenance-${MODE}-${RUN_ID}.json"
run_err="${run_json%.json}.stderr"
set +e
python3 "${LANE_SCRIPT}" "${MODE}" --json "$@" >"${run_json}.tmp" 2>"${run_err}"
cmd_rc=$?
set -e
mv "${run_json}.tmp" "${run_json}"
cp "${run_json}" "${LAST_JSON}"
if [[ -s "${run_err}" ]]; then
    log "stderr captured at ${run_err}"
    python3 - "${run_err}" "${LOG_FILE}" <<'PY'
from pathlib import Path
import sys
err = Path(sys.argv[1]).read_text(errors='replace').splitlines()[:120]
with open(sys.argv[2], 'a') as out:
    for line in err:
        print(line, file=out)
PY
fi
log "json_report=${run_json}"
python3 - "${run_json}" "${LOG_FILE}" <<'PY'
from pathlib import Path
import sys
with open(sys.argv[2], 'a') as out:
    out.write(Path(sys.argv[1]).read_text(errors='replace'))
    out.write('\n')
PY
status="$(json_status "${run_json}")"
log "status=${status} command_exit=${cmd_rc}"
if [[ "${cmd_rc}" != "0" || "${status}" != "ok" ]]; then
    log "=== Hermes skill maintenance ${MODE} END: FAILED ==="
    exit 1
fi
log "=== Hermes skill maintenance ${MODE} END: OK ==="
