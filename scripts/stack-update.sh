#!/bin/bash
# hermes-webui-stack daily update — zero-patch model
# Pulls upstream images, rebuilds local images, applies if anything changed.
# Safe: docker compose up -d only recreates containers whose image/config actually changed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(dirname "$SCRIPT_DIR")"
cd "$STACK_DIR"

# Include Caddy overlay in all compose commands
# Caddy shares Tailscale's network namespace via network_mode: service:tailscale
export COMPOSE_FILE="docker-compose.yml:docker-compose.caddy.yml:docker-compose.override.yml"

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "=== Stack Update: $TIMESTAMP ==="

# 1. Disk space preflight
ROOT_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
DOCKER_PCT=$(df /var/lib/docker 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%' || echo 0)
echo "Disk: root=${ROOT_PCT}% docker=${DOCKER_PCT}%"
if [ "$ROOT_PCT" -gt 90 ]; then
    echo "BLOCKED: root filesystem at ${ROOT_PCT}% — bailing before update"
    exit 1
fi

# 2. Capture pre-update state
PRE_HASH=$(docker compose config --images 2>/dev/null | sort | xargs -n 1 docker images -q 2>/dev/null | sort | sha256sum | cut -c1-12)
PRE_CONTAINERS=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | sort)

# 3. Pull upstream images
echo "--- Pulling upstream images ---"
docker compose pull --ignore-pull-failures 2>&1 || echo "(some pulls failed — expected for local images)"

# 4. Rebuild local images (layer cache makes this fast when nothing changed)
echo "--- Rebuilding local images ---"
LOCAL_IMAGES=(
    "Dockerfile.gateway-dev:local/hermes-webui-stack-gateway-dev:latest"
    "Dockerfile.webui:local/hermes-webui-stack-webui:latest"
    "Dockerfile.helm:local/hermes-webui-stack-helm:latest"
    "Dockerfile.agenthub-api:local/hermes-webui-stack-agenthub-api:latest"
)
for entry in "${LOCAL_IMAGES[@]}"; do
    dockerfile="${entry%%:*}"
    image="${entry##*:}"
    if [ -f "$dockerfile" ]; then
        echo "  Building $image from $dockerfile..."
        docker build -t "$image" -f "$dockerfile" . 2>&1 | tail -3
    else
        echo "  SKIP: $dockerfile not found"
    fi
done

# 5. Check if anything actually changed
POST_HASH=$(docker compose config --images 2>/dev/null | sort | xargs -n 1 docker images -q 2>/dev/null | sort | sha256sum | cut -c1-12)
if [ "$PRE_HASH" = "$POST_HASH" ]; then
    echo "No image changes detected."
    # Still check Caddy namespace alignment (Tailscale may have been recreated out-of-band)
    echo "--- Caddy namespace check (no-update path) ---"
    TS_ID=$(docker inspect hermes-webui-stack-tailscale --format '{{.Id}}' 2>/dev/null || true)
    CADDY_NET=$(docker inspect hermes-webui-stack-caddy --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || true)
    if [ -n "$TS_ID" ] && [ "$CADDY_NET" != "container:$TS_ID" ]; then
        echo "  WARNING: Caddy namespace mismatch detected even without image changes."
        echo "  Expected: container:$TS_ID"
        echo "  Actual:   $CADDY_NET"
        echo "  Reconciling Caddy..."
        docker compose up -d --force-recreate --no-deps caddy 2>&1
        sleep 5
    else
        echo "  Caddy namespace OK."
    fi
    echo "=== Update complete (no image changes) ==="
    exit 0
fi
echo "Image changes detected (pre=$PRE_HASH post=$POST_HASH). Applying..."

# 6. Apply (only recreates containers whose image/config changed)
echo "--- Applying changes ---"
docker compose up -d 2>&1

# 6b. Reconcile Caddy namespace after apply
# Caddy shares Tailscale's network via network_mode: service:tailscale.
# If Tailscale was recreated (new container ID), Caddy must follow.
echo "--- Caddy namespace reconciliation ---"
TS_ID=$(docker inspect hermes-webui-stack-tailscale --format '{{.Id}}' 2>/dev/null || true)
CADDY_NET=$(docker inspect hermes-webui-stack-caddy --format '{{.HostConfig.NetworkMode}}' 2>/dev/null || true)
if [ -n "$TS_ID" ] && [ "$CADDY_NET" != "container:$TS_ID" ]; then
    echo "  Caddy namespace mismatch! Reconciling..."
    echo "  Expected: container:$TS_ID"
    echo "  Actual:   $CADDY_NET"
    docker compose up -d --force-recreate --no-deps caddy 2>&1
    sleep 5
else
    echo "  Caddy namespace OK: $CADDY_NET"
fi

# 7. Wait for containers to settle
echo "--- Waiting for health checks (15s) ---"
sleep 15

# 8. Verify Hermes Agent runtime contract before shallow HTTP probes.
# WebUI runs Hermes in its own Python 3.12 venv; it must match the gateway image.
echo "--- Hermes Agent version contract ---"
WEBUI_AGENT_VERSION=""
GATEWAY_AGENT_VERSION=""
for _ in $(seq 1 36); do
    WEBUI_AGENT_VERSION=$(docker exec hermes-webui-stack-webui \
        /app/venv/bin/python -c 'import importlib.metadata as m; print(m.version("hermes-agent"))' \
        2>/dev/null || true)
    GATEWAY_AGENT_VERSION=$(docker exec hermes-webui-stack-gateway-infrastructure \
        /opt/hermes/.venv/bin/python3 -c 'import importlib.metadata as m; print(m.version("hermes-agent"))' \
        2>/dev/null || true)
    if [ -n "$WEBUI_AGENT_VERSION" ] && [ -n "$GATEWAY_AGENT_VERSION" ]; then
        break
    fi
    sleep 5
done
echo "  webui:  ${WEBUI_AGENT_VERSION:-unavailable}"
echo "  gateway: ${GATEWAY_AGENT_VERSION:-unavailable}"
if [ -z "$WEBUI_AGENT_VERSION" ] || [ -z "$GATEWAY_AGENT_VERSION" ]; then
    echo "BLOCKED: could not resolve Hermes Agent versions after 180s"
    exit 1
fi
if [ "$WEBUI_AGENT_VERSION" != "$GATEWAY_AGENT_VERSION" ]; then
    echo "BLOCKED: Hermes Agent version skew (webui=$WEBUI_AGENT_VERSION gateway=$GATEWAY_AGENT_VERSION)"
    exit 1
fi

# 9. Show container state
docker compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null

# 10. Check for unhealthy containers
UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ci 'unhealthy\|restarting\|exited' || true)
if [ "$UNHEALTHY" -gt 0 ]; then
    echo "WARNING: $UNHEALTHY container(s) unhealthy/restarting/exited"
    docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -i 'unhealthy\|restarting\|exited'
fi

# 11. Quick HTTP smoke test
echo "--- HTTP smoke tests ---"
for check in \
    "webui:http://127.0.0.1:8787/" \
    "dashboard:http://127.0.0.1:9119/" \
    "helm:http://127.0.0.1:7890/helm/" \
    "caddy-healthz:http://127.0.0.1:9120/healthz"; do
    svc="${check%%:*}"
    url="${check##*:}"
    code=$(docker exec hermes-webui-stack-tailscale wget -q -O /dev/null -S "$url" 2>&1 | grep 'HTTP/' | tail -1 | awk '{print $2}' || echo "ERR")
    echo "  $svc: $code"
done

echo "=== Update complete: $TIMESTAMP ==="
