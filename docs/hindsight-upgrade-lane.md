# Hindsight Upgrade Lane

Hindsight is intentionally excluded from the routine Hermes WebUI/gateway upgrade lane.
It is stateful, uses `/srv/hindsight-pg0` mounted at `/home/hindsight/.pg0`, and has a
separate backup/rollback blast radius.

## Current policy

- Routine `hermes-autonomous-upgrade.sh` with no args: candidate validation only for WebUI/gateway images.
- Explicit `hermes-autonomous-upgrade.sh --apply`: applies the latest green WebUI/gateway candidate.
- Neither path recreates `hindsight`; `stack_upgrade_v2.py` keeps `hindsight` in `FORBIDDEN_AUTOMATED_SERVICES`.

## Current state observed 2026-06-08

- Running image: `hindsight-with-st:latest`
- Version label: `0.7.0-slim`
- Revision label: `99525144b257e827ff07e98665eddd7000b8fc3c`
- Newer upstream tags observed: `v0.7.1`, `v0.7.2`
- Upstream main observed: `b708302187a4abf153b88bcfe1db4007d453f8ba`

## Stateful upgrade checklist

Do not run this from the normal WebUI/gateway updater. Use a maintenance window.

1. Pick target tag/revision, normally latest stable tag rather than main.
2. Verify live health and current image ID:
   ```bash
   docker ps --filter name=hermes-webui-stack-hindsight
   docker image inspect hindsight-with-st:latest
   ```
3. Verify fresh pg0 backup marker and archive integrity:
   ```bash
   sudo -n -p '' cat /var/lib/hindsight-pg0-backup/last-success.json
   systemctl status hindsight-pg0-backup.service --no-pager
   ```
4. Build or pull a candidate Hindsight image under a non-latest tag.
5. Smoke the candidate against temporary data, not `/srv/hindsight-pg0`.
6. If smoke passes and maintenance window is approved:
   - tag candidate to `hindsight-with-st:latest`
   - recreate only `hindsight`
   - verify `/health`, memory-provider calls, and pg0 mount
7. Roll back by retagging the previous image ID and recreating only `hindsight`.

## Non-goals

- Do not silently fold Hindsight into routine WebUI/gateway upgrades.
- Do not recreate Hindsight just because WebUI/gateway candidate validation is green.
- Do not prune Hindsight data or backups without explicit approval.
