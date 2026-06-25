#!/bin/bash
# Weekly cleanup for hermes-webui-stack root filesystem
# Frees space from: upgrade backups, Docker build cache, logs

set -euo pipefail

LOGFILE="/var/log/hermes-weekly-cleanup.log"
exec >>"$LOGFILE" 2>>1

echo "=== $(date -Iseconds) Weekly cleanup starting ==="

# 1. Prune old upgrade backups (keep 2 most recent timestamped sets + manual)
BACKUP_ROOT="/home/jmelchiori/docker-compose/hermes-webui-stack/backups/upgrade-v2"
if [ -d "$BACKUP_ROOT" ]; then
    cd "$BACKUP_ROOT"
    mapfile -t OLD_BACKUPS < <(ls -d [0-9]*T[0-9]*Z 2>/dev/null | sort | head -n -2)
    for d in "${OLD_BACKUPS[@]}"; do
        if [ -d "$d" ]; then
            if [ -f "$d/manifest.json" ] && [ "$(stat -c '%U' "$d/manifest.json" 2>/dev/null)" = "root" ]; then
                sudo -n -p '' rm -rf "$d"
            else
                rm -rf "$d"
            fi
            echo "Removed old backup: $d"
        fi
    done
fi

# 2. Prune Docker build cache older than 7 days
if command -v docker >/dev/null 2>&1; then
    docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
    echo "Pruned Docker build cache older than 7 days"
fi

# 3. Truncate auth logs if they exceed 500MB
for logfile in /var/log/auth.log /var/log/auth.log.1; do
    if [ -f "$logfile" ] && [ "$(stat -c '%s' "$logfile" 2>/dev/null || echo 0)" -gt 524288000 ]; then
        sudo -n -p '' truncate -s 0 "$logfile"
        echo "Truncated $logfile"
    fi
done

# 4. Prune old upgrade state JSON reports (keep 14 days)
STATE_DIR="/home/jmelchiori/docker-compose/hermes-webui-stack/state/upgrade-v2"
if [ -d "$STATE_DIR" ]; then
    find "$STATE_DIR" -maxdepth 1 -name 'autonomous-*.json' -type f -mtime +14 -delete 2>/dev/null || true
    find "$STATE_DIR" -maxdepth 1 -name 'manual-*.json' -type f -mtime +14 -delete 2>/dev/null || true
    echo "Pruned old state reports >14 days"
fi

ROOT_FREE=$(df -B1 / | awk 'NR==2{print $4}')
ROOT_FREE_GIB=$(echo "scale=2; $ROOT_FREE / 1024 / 1024 / 1024" | bc)
echo "Root free space: ${ROOT_FREE_GIB}GiB"
echo "=== $(date -Iseconds) Weekly cleanup complete ==="
