#!/bin/bash
# hermes-webui-stack daily update — zero-patch model
# Pulls upstream images, rebuilds local images, applies if anything changed.
# Safe: docker compose up -d only recreates containers whose image/config actually changed.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="$(dirname "$SCRIPT_DIR")"
cd "$STACK_DIR"

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
PRE_HASH=$(docker compose config --images 2>/dev/null | sort | xargs docker images -q 2>/dev/null | sort | sha256sum | cut -c1-12)
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
POST_HASH=$(docker compose config --images 2>/dev/null | sort | xargs docker images -q 2>/dev/null | sort | sha256sum | cut -c1-12)
if [ "$PRE_HASH" = "$POST_HASH" ]; then
    echo "No image changes detected. Nothing to apply."
    echo "=== Update complete (no changes) ==="
    exit 0
fi
echo "Image changes detected (pre=$PRE_HASH post=$POST_HASH). Applying..."

# 6. Apply (only recreates containers whose image/config changed)
echo "--- Applying changes ---"
docker compose up -d 2>&1

# 7. Wait for containers to settle
echo "--- Waiting for health checks (15s) ---"
sleep 15

# 8. Verify
echo "--- Post-apply verification ---"
docker compose ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null

# 9. Check for unhealthy containers
UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ci 'unhealthy\|restarting\|exited' || true)
if [ "$UNHEALTHY" -gt 0 ]; then
    echo "WARNING: $UNHEALTHY container(s) unhealthy/restarting/exited"
    docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -i 'unhealthy\|restarting\|exited'
fi

# 10. Quick HTTP smoke test
echo "--- HTTP smoke tests ---"
for check in \
    "webui:http://127.0.0.1:8787/" \
    "dashboard:http://127.0.0.1:9119/" \
    "helm:http://127.0.0.1:7890/helm/"; do
    svc="${check%%:*}"
    url="${check##*:}"
    code=$(docker exec hermes-webui-stack-tailscale wget -q -O /dev/null -S "$url" 2>&1 | grep 'HTTP/' | tail -1 | awk '{print $2}' || echo "ERR")
    echo "  $svc: $code"
done

echo "=== Update complete: $TIMESTAMP ==="
