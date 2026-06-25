#!/bin/bash
# pre-apply-check.sh — Check for active sessions before container recreation
# Usage: ./pre-apply-check.sh              # single check, exit 0=idle
#        ./pre-apply-check.sh --wait 600    # poll until idle, timeout N seconds

set -euo pipefail

THRESHOLD_MIN=${THRESHOLD_MIN:-15}
POLL_INTERVAL=${POLL_INTERVAL:-30}
WEBUI_CONTAINER="hermes-webui-stack-webui"

check_webui() {
    local count
    count=$(docker exec "$WEBUI_CONTAINER" \
        find /home/hermeswebui/.hermes/webui-mvp/sessions \
        -name "*.json" -not -name "_index.json" \
        -mmin "-${THRESHOLD_MIN}" 2>/dev/null | wc -l)
    if [ "$count" -gt 0 ]; then
        echo "  Active WebUI session(s):"
        docker exec "$WEBUI_CONTAINER" bash -c "
            NOW=\$(date +%s)
            for f in /home/hermeswebui/.hermes/webui-mvp/sessions/*.json; do
                NAME=\$(basename \"\$f\" .json)
                [ \"\$NAME\" = \"_index\" ] && continue
                MTIME=\$(stat -c %Y \"\$f\" 2>/dev/null)
                AGE=\$(( (NOW - MTIME) / 60 ))
                [ \$AGE -lt ${THRESHOLD_MIN} ] && printf \"    %-16s  %d min ago  (%d KB)\\n\" \"\$NAME\" \"\$AGE\" \$(( \$(stat -c %s \"\$f\") / 1024 ))
            done
        " 2>/dev/null || true
        return 1
    fi
    return 0
}

check_gateways() {
    local any_active=false
    while IFS= read -r gw; do
        local count
        count=$(docker exec "$gw" ps aux 2>/dev/null | grep -cE "run_agent|agent\.py" || true)
        if [ "$count" -gt 0 ]; then
            echo "  $gw: $count agent process(es)"
            any_active=true
        fi
    done < <(docker ps --filter name=gateway --format '{{.Names}}' 2>/dev/null)
    $any_active && return 1 || return 0
}

check_dashboard() {
    docker exec hermes-webui-stack-dashboard python3 -c "
import json, os, time, sys
f = '/home/hermeswebui/.hermes/gateway_state.json'
if os.path.exists(f):
    mtime = os.path.getmtime(f)
    age_m = (time.time() - mtime) / 60
    if age_m < 60:
        with open(f) as fh:
            data = json.load(fh)
        active = data.get('active_agents', [])
        print(f'  {len(active)} active_agents (state file: {age_m:.0f} min old)')
        sys.exit(len(active))
    else:
        print(f'  state file stale ({age_m:.0f} min old)')
        sys.exit(0)
else:
    print('  no gateway_state.json')
    sys.exit(0)
" 2>/dev/null || return 1
    return 0
}

# --- Main ---
if [ "${1:-}" = "--wait" ]; then
    TIMEOUT="${2:-300}"
    END=$(( $(date +%s) + TIMEOUT ))
    echo "Waiting up to ${TIMEOUT}s for idle..."
    while [ $(date +%s) -lt $END ]; do
        result=$(check_webui 2>&1) && w_ok=1 || w_ok=0
        [ "$w_ok" -eq 1 ] && echo "OK — no active sessions" && exit 0
        sleep "$POLL_INTERVAL"
    done
    echo "TIMEOUT — active sessions still present after ${TIMEOUT}s"
    check_webui
    exit 2
fi

echo "=== Pre-Apply Session Check ==="
echo ""
echo "WebUI sessions (last ${THRESHOLD_MIN}min):"
if check_webui; then
    echo "  (idle)"
    w_result=0
else
    echo "  ACTIVE"
    w_result=1
fi

echo ""
echo "Gateway agents:"
if check_gateways; then
    echo "  (no active agents)"
    g_result=0
else
    g_result=1
fi

echo ""
echo "Dashboard:"
if check_dashboard; then
    d_result=0
else
    d_result=1
fi

echo ""
if [ "$w_result" -eq 0 ] && [ "$g_result" -eq 0 ] && [ "$d_result" -eq 0 ]; then
    echo "RESULT: IDLE — safe to proceed"
    exit 0
else
    echo "RESULT: ACTIVE — wait or investigate before proceeding"
    exit 1
fi
