#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/hermes-profile-cron-ticks.lock"
LOG_TS() { date --iso-8601=seconds; }
PROFILES=(default buyer developer family financial infrastructure personal trading)

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(LOG_TS) profile cron tick skipped: lock busy"
  exit 0
fi

for profile in "${PROFILES[@]}"; do
  if [[ "$profile" == "default" ]]; then
    profile_home="/opt/data"
  else
    profile_home="/opt/data/profiles/${profile}"
  fi

  echo "$(LOG_TS) ticking profile=${profile} HERMES_HOME=${profile_home}"
  docker exec hermes-webui-stack-gateway sh -lc \
    "HERMES_HOME=${profile_home} HERMES_CRON_TIMEOUT=1800 /opt/hermes/.venv/bin/hermes cron tick" \
    || echo "$(LOG_TS) profile=${profile} cron tick failed"
done
