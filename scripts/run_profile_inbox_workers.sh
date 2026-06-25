#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/hermes-profile-inbox-workers.lock"
LOG_TS() { date --iso-8601=seconds; }
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
STACK_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$STACK_ROOT/config/agenthub-client.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$STACK_ROOT/config/agenthub-client.env"
  set +a
fi
PROFILE_ROOT="${PROFILE_ROOT:-$STACK_ROOT/hermes-home/profiles}"
HOST_PROFILE_ROOTS=(
  "$PROFILE_ROOT"
  "$STACK_ROOT/hermes-home/profiles"
  "$STACK_ROOT/profiles"
  "$STACK_ROOT/workspace/.hermes/profiles"
)
PROFILE_ROOT_FOUND=0
for candidate in "${HOST_PROFILE_ROOTS[@]}"; do
  if [[ -d "$candidate" ]] && [[ -n "$(ls -A "$candidate" 2>/dev/null)" ]]; then
    PROFILE_ROOT="$candidate"
    PROFILE_ROOT_FOUND=1
    break
  fi
done
if [[ "$PROFILE_ROOT_FOUND" -ne 1 ]]; then
  # Host no longer carries profile directories after runtime-layout migration.
  # Use the container-mounted profile root for discovery; all actual work is
  # docker exec'd into per-profile gateway containers anyway.
  PROFILE_ROOT="/opt/data/profiles"
  PROFILE_DISCOVERY_IN_CONTAINER=1
else
  PROFILE_DISCOVERY_IN_CONTAINER=0
fi
MAILBOXES=()
GATEWAY_CONTAINER="${GATEWAY_CONTAINER:-hermes-webui-stack-gateway}"
PROFILE_DISCOVERY_CONTAINER="${PROFILE_DISCOVERY_CONTAINER:-hermes-webui-stack-gateway-developer}"
DOCKER_EXEC_TIMEOUT_SECONDS="${DOCKER_EXEC_TIMEOUT_SECONDS:-1800}"
DOCKER_EXEC_RETRY_SECONDS="${DOCKER_EXEC_RETRY_SECONDS:-5}"
DOCKER_EXEC_MAX_RETRIES="${DOCKER_EXEC_MAX_RETRIES:-3}"
RECLAIM_STALE_SECONDS="${RECLAIM_STALE_SECONDS:-3600}"
STALE_SWEEP_AGE_MINUTES="${STALE_SWEEP_AGE_MINUTES:-30}"
STALE_SWEEP_MAX_RECORDS="${STALE_SWEEP_MAX_RECORDS:-50}"
DEFAULT_DELEGATION_CONCURRENCY="${DEFAULT_DELEGATION_CONCURRENCY:-1}"
# Preserve the current higher-throughput default for developer unless explicitly
# overridden, but let every profile opt in via <PROFILE>_DELEGATION_CONCURRENCY.
DEVELOPER_DELEGATION_CONCURRENCY="${DEVELOPER_DELEGATION_CONCURRENCY:-2}"
INFRASTRUCTURE_DELEGATION_CONCURRENCY="${INFRASTRUCTURE_DELEGATION_CONCURRENCY:-1}"
AGENTHUB_WORKER_ROUTING="${AGENTHUB_WORKER_ROUTING:-api}"
# Comma/space-separated list of profiles that should run delegations after the
# shared batch lock is released. Default "all" means every discovered profile.
LONG_DELEGATION_PROFILES_CONFIG="${LONG_DELEGATION_PROFILES:-all}"

PROFILES=()
LONG_DELEGATION_PROFILES=()

load_mailboxes() {
  if [[ -n "${AGENTHUB_WORKER_MAILBOXES:-}" ]]; then
    read -r -a MAILBOXES <<<"${AGENTHUB_WORKER_MAILBOXES//,/ }"
  elif [[ "$AGENTHUB_WORKER_ROUTING" == "api" ]]; then
    # Once delegations are routed through AgentHub API, the legacy Hermes
    # SessionDB mailbox adapter is no longer authoritative for delegation work.
    # Avoid the known-bad inbox/replies reclaim/claim loop on live SessionDB
    # versions that do not expose mailbox claim methods.
    MAILBOXES=(delegations)
  else
    MAILBOXES=(inbox delegations replies)
  fi
}

load_profiles() {
  local dir profile_list profile
  PROFILES=()

  if [[ "${PROFILE_DISCOVERY_IN_CONTAINER:-0}" -eq 1 ]]; then
    if ! docker container inspect "$PROFILE_DISCOVERY_CONTAINER" >/dev/null 2>&1; then
      echo "$(LOG_TS) profile discovery container missing: $PROFILE_DISCOVERY_CONTAINER" >&2
      exit 1
    fi
    profile_list="$(docker exec "$PROFILE_DISCOVERY_CONTAINER" sh -lc "find '$PROFILE_ROOT' -maxdepth 1 -mindepth 1 -type d -printf '%f\n' 2>/dev/null | sort")"
    while IFS= read -r profile; do
      [[ -n "$profile" ]] || continue
      PROFILES+=("$profile")
    done <<<"$profile_list"
  else
    if [[ ! -d "$PROFILE_ROOT" ]]; then
      echo "$(LOG_TS) profile root missing: $PROFILE_ROOT" >&2
      exit 1
    fi
    shopt -s nullglob
    for dir in "$PROFILE_ROOT"/*; do
      [[ -d "$dir" ]] || continue
      PROFILES+=("$(basename "$dir")")
    done
    shopt -u nullglob
  fi

  if [[ "${#PROFILES[@]}" -eq 0 ]]; then
    echo "$(LOG_TS) no profiles discovered under $PROFILE_ROOT" >&2
    exit 1
  fi
}

profile_exists() {
  local profile="$1"
  local candidate
  for candidate in "${PROFILES[@]}"; do
    if [[ "$candidate" == "$profile" ]]; then
      return 0
    fi
  done
  return 1
}

load_long_delegation_profiles() {
  local requested profile
  declare -A seen=()
  LONG_DELEGATION_PROFILES=()

  if [[ -z "$LONG_DELEGATION_PROFILES_CONFIG" || "$LONG_DELEGATION_PROFILES_CONFIG" == "all" ]]; then
    LONG_DELEGATION_PROFILES=("${PROFILES[@]}")
    return
  fi

  if [[ "$LONG_DELEGATION_PROFILES_CONFIG" == "none" ]]; then
    LONG_DELEGATION_PROFILES=()
    return
  fi

  requested="${LONG_DELEGATION_PROFILES_CONFIG//,/ }"
  for profile in $requested; do
    [[ -n "$profile" ]] || continue
    if ! profile_exists "$profile"; then
      echo "$(LOG_TS) skipping unknown long-delegation profile=${profile}" >&2
      continue
    fi
    if [[ -n "${seen[$profile]:-}" ]]; then
      continue
    fi
    seen[$profile]=1
    LONG_DELEGATION_PROFILES+=("$profile")
  done

  if [[ "${#LONG_DELEGATION_PROFILES[@]}" -eq 0 ]]; then
    echo "$(LOG_TS) no valid long-delegation profiles configured; defaulting to all discovered profiles" >&2
    LONG_DELEGATION_PROFILES=("${PROFILES[@]}")
  fi
}

profile_has_long_delegation_lane() {
  local profile="$1"
  local candidate
  for candidate in "${LONG_DELEGATION_PROFILES[@]}"; do
    if [[ "$profile" == "$candidate" ]]; then
      return 0
    fi
  done
  return 1
}

delegation_concurrency_for_profile() {
  local profile="$1"
  local normalized_profile
  local var_name
  local fallback="$DEFAULT_DELEGATION_CONCURRENCY"

  case "$profile" in
    developer) fallback="$DEVELOPER_DELEGATION_CONCURRENCY" ;;
    infrastructure) fallback="$INFRASTRUCTURE_DELEGATION_CONCURRENCY" ;;
  esac

  normalized_profile="$(printf '%s' "$profile" | tr '[:lower:]-' '[:upper:]_')"
  var_name="${normalized_profile}_DELEGATION_CONCURRENCY"
  echo "${!var_name:-$fallback}"
}

run_mailbox_worker() {
  local profile="$1"
  local mailbox="$2"
  local attempt output rc

  for ((attempt=1; attempt<=DOCKER_EXEC_MAX_RETRIES; attempt++)); do
    if ! docker container inspect "$GATEWAY_CONTAINER" >/dev/null 2>&1; then
      echo "$(LOG_TS) gateway container missing before profile=${profile} mailbox=${mailbox} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    output=""
    if output=$(timeout --signal=TERM --kill-after=30s "$DOCKER_EXEC_TIMEOUT_SECONDS" \
      docker exec "$GATEWAY_CONTAINER" sh -lc \
      "/opt/hermes/.venv/bin/python /workspace/AgentHub/scripts/process_profile_inbox.py --profile ${profile} --mailbox ${mailbox} --max-messages 5 --reclaim-stale ${RECLAIM_STALE_SECONDS}" 2>&1); then
      [[ -n "$output" ]] && printf '%s\n' "$output"
      return 0
    fi

    rc=$?
    [[ -n "$output" ]] && printf '%s\n' "$output"

    if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
      echo "$(LOG_TS) profile=${profile} mailbox=${mailbox} worker timed out after ${DOCKER_EXEC_TIMEOUT_SECONDS}s on attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      if (( attempt < DOCKER_EXEC_MAX_RETRIES )); then
        sleep "$DOCKER_EXEC_RETRY_SECONDS"
        continue
      fi
      return "$rc"
    fi

    if grep -qE 'No such container|unable to upgrade to tcp, received 409' <<<"$output"; then
      echo "$(LOG_TS) transient gateway exec failure for profile=${profile} mailbox=${mailbox} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}; retrying"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    return "$rc"
  done

  echo "$(LOG_TS) profile=${profile} mailbox=${mailbox} worker exhausted retries"
  return 1
}

run_agenthub_worker() {
  local profile="$1"
  local attempt output rc
  local -a env_args=()
  local var
  for var in AGENTHUB_API_BASE_URL AGENTHUB_API_TOKEN_FILE AGENTHUB_ROUTING_BACKEND AGENTHUB_WORKER_ROUTING AGENTHUB_WORKER_CAPABILITIES; do
    if [[ -n "${!var:-}" ]]; then
      env_args+=("-e" "${var}=${!var}")
    fi
  done

  for ((attempt=1; attempt<=DOCKER_EXEC_MAX_RETRIES; attempt++)); do
    if ! docker container inspect "$GATEWAY_CONTAINER" >/dev/null 2>&1; then
      echo "$(LOG_TS) gateway container missing before AgentHub API worker profile=${profile} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    output=""
    if output=$(timeout --signal=TERM --kill-after=30s "$DOCKER_EXEC_TIMEOUT_SECONDS" \
      docker exec "${env_args[@]}" "$GATEWAY_CONTAINER" sh -lc \
      "/opt/hermes/.venv/bin/python /workspace/AgentHub/scripts/process_agenthub_deliveries.py --profile ${profile} --max-messages 1" 2>&1); then
      [[ -n "$output" ]] && printf '%s\n' "$output"
      return 0
    fi

    rc=$?
    [[ -n "$output" ]] && printf '%s\n' "$output"

    if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
      echo "$(LOG_TS) AgentHub API worker profile=${profile} timed out after ${DOCKER_EXEC_TIMEOUT_SECONDS}s on attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      if (( attempt < DOCKER_EXEC_MAX_RETRIES )); then
        sleep "$DOCKER_EXEC_RETRY_SECONDS"
        continue
      fi
      return "$rc"
    fi

    if grep -qE 'No such container|unable to upgrade to tcp, received 409' <<<"$output"; then
      echo "$(LOG_TS) transient gateway exec failure for AgentHub API worker profile=${profile} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}; retrying"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    return "$rc"
  done

  echo "$(LOG_TS) AgentHub API worker profile=${profile} exhausted retries"
  return 1
}

run_delegation_worker() {
  local profile="$1"
  if [[ "$AGENTHUB_WORKER_ROUTING" == "api" ]]; then
    run_agenthub_worker "$profile"
  elif [[ "$AGENTHUB_WORKER_ROUTING" == "mailbox" ]]; then
    run_mailbox_worker "$profile" delegations
  else
    echo "$(LOG_TS) unsupported AGENTHUB_WORKER_ROUTING=${AGENTHUB_WORKER_ROUTING} for profile=${profile}" >&2
    return 2
  fi
}

run_stale_sweep() {
  local profile="$1"
  local attempt output rc

  for ((attempt=1; attempt<=DOCKER_EXEC_MAX_RETRIES; attempt++)); do
    if ! docker container inspect "$GATEWAY_CONTAINER" >/dev/null 2>&1; then
      echo "$(LOG_TS) gateway container missing before stale sweep profile=${profile} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    output=""
    if output=$(timeout --signal=TERM --kill-after=30s "$DOCKER_EXEC_TIMEOUT_SECONDS" \
      docker exec "$GATEWAY_CONTAINER" sh -lc \
      "/opt/hermes/.venv/bin/python /workspace/AgentHub/scripts/reconcile_stale_delegations.py --sender-profile ${profile} --age-threshold-minutes ${STALE_SWEEP_AGE_MINUTES} --max-records ${STALE_SWEEP_MAX_RECORDS}" 2>&1); then
      [[ -n "$output" ]] && printf '%s\n' "$output"
      return 0
    fi

    rc=$?
    [[ -n "$output" ]] && printf '%s\n' "$output"

    if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
      echo "$(LOG_TS) stale sweep profile=${profile} timed out after ${DOCKER_EXEC_TIMEOUT_SECONDS}s on attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}"
      if (( attempt < DOCKER_EXEC_MAX_RETRIES )); then
        sleep "$DOCKER_EXEC_RETRY_SECONDS"
        continue
      fi
      return "$rc"
    fi

    if grep -qE 'No such container|unable to upgrade to tcp, received 409' <<<"$output"; then
      echo "$(LOG_TS) transient gateway exec failure for stale sweep profile=${profile} attempt=${attempt}/${DOCKER_EXEC_MAX_RETRIES}; retrying"
      sleep "$DOCKER_EXEC_RETRY_SECONDS"
      continue
    fi

    return "$rc"
  done

  echo "$(LOG_TS) stale sweep profile=${profile} exhausted retries"
  return 1
}

run_long_delegation_profile() {
  local profile="$1"
  local mailbox="delegations"
  local lock_file="/tmp/hermes-${profile}-delegations.lock"

  (
    flock -n 8 || {
      echo "$(LOG_TS) ${profile} delegations skipped: lock busy"
      exit 0
    }

    local concurrency
    concurrency="$(delegation_concurrency_for_profile "$profile")"
    if [[ "$concurrency" -gt 1 ]]; then
      echo "$(LOG_TS) ${profile} delegations concurrency=${concurrency}"
      local pids=()
      local failed=0
      local i pid
      for i in $(seq 1 "$concurrency"); do
        echo "$(LOG_TS) processing profile=${profile} mailbox=${mailbox} worker=${i}/${concurrency}"
        run_delegation_worker "$profile" &
        pids+=($!)
      done
      for pid in "${pids[@]}"; do
        if ! wait "$pid"; then
          failed=1
        fi
      done
      if [[ "$failed" -ne 0 ]]; then
        echo "$(LOG_TS) ${profile} delegation concurrency=${concurrency} had a worker failure"
      fi
      echo "$(LOG_TS) ${profile} delegations concurrency=${concurrency} complete"
    else
      echo "$(LOG_TS) processing profile=${profile} mailbox=${mailbox}"
      run_delegation_worker "$profile" || echo "$(LOG_TS) profile=${profile} mailbox=${mailbox} worker failed"
    fi
  ) 8>"$lock_file"
}

load_mailboxes
load_profiles
load_long_delegation_profiles

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(LOG_TS) profile inbox worker run skipped: lock busy"
  exit 0
fi

for profile in "${PROFILES[@]}"; do
  GATEWAY_CONTAINER="hermes-webui-stack-gateway-${profile}"
  for mailbox in "${MAILBOXES[@]}"; do
    if [[ "$mailbox" == "delegations" ]]; then
      if profile_has_long_delegation_lane "$profile"; then
        continue
      fi
      echo "$(LOG_TS) processing profile=${profile} mailbox=${mailbox} routing=${AGENTHUB_WORKER_ROUTING}"
      run_delegation_worker "$profile" || echo "$(LOG_TS) profile=${profile} mailbox=${mailbox} worker failed"
      continue
    fi
    echo "$(LOG_TS) processing profile=${profile} mailbox=${mailbox}"
    run_mailbox_worker "$profile" "$mailbox" || echo "$(LOG_TS) profile=${profile} mailbox=${mailbox} worker failed"
  done
done

for profile in "${PROFILES[@]}"; do
  GATEWAY_CONTAINER="hermes-webui-stack-gateway-${profile}"
  echo "$(LOG_TS) reconciling stale delegations profile=${profile}"
  run_stale_sweep "$profile" || echo "$(LOG_TS) profile=${profile} stale reconciliation failed"
done

# Release the shared batch lock before long-lived delegations so future scheduler
# ticks can keep servicing inbox/replies and short delegation lanes.
exec 9>&-

for profile in "${LONG_DELEGATION_PROFILES[@]}"; do
  GATEWAY_CONTAINER="hermes-webui-stack-gateway-${profile}"
  run_long_delegation_profile "$profile"
done
