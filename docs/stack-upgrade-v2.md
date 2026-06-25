# hermes-webui-stack upgrade v2

Status: installed as a staged host-side workflow. The scheduled/automatic portion may reconcile Hermes source in git, but it **does not** build, apply, restart, or recreate containers unless `apply --confirm RECREATE-HERMES-STACK` is explicitly invoked.

## Why this exists

The old upgrade path mixed source reconciliation, patch extraction, image build, rollout, and verification into one process. When private Hermes source was behind `upstream/main` or private patches no longer applied cleanly, failure surfaced late during build/rollout. That made the scheduler look like it ran while the actual upgrade was unsafe or incomplete.

The v2 process separates the workflow into hard gates:

1. **Source reconciliation** — isolated worktree replay of private commits onto `upstream/main`, Dockerized syntax/test verification, then optional promotion into the workspace/build mirrors. If deterministic replay cannot solve it, the script submits a verified-developer AgentHub delegation with the conflict/test artifacts.
2. **Preflight** — health, storage, backup, source, and image checks. The scheduled job may run source reconciliation first; the preflight itself remains read-only.
3. **SQLite hot backup** — host-side backups of stack SQLite databases before any rollout, with backup-root storage guardrails and bounded retention.
4. **Candidate build** — immutable candidate tags, no running container changes.
5. **Apply** — explicit confirmation only; retag candidate to `:latest`, recreate selected services, verify, and rollback image tags/recreate if verification fails.

## Operator safety contract

Until an apply is deliberately approved:

- Do not restart, recreate, or rebuild live containers.
- Do not run Python imports or application commands inside live gateway/webui/helm containers.
- Do not use `docker exec` into application containers for inspection unless there is a specific incident-response need and the command is side-effect understood.
- Prefer host-side Docker metadata commands: `docker compose ps`, `docker inspect`, `docker diff`, `docker image inspect`.
- Treat source reconciliation as a gate with evidence: isolated worktree, explicit skipped/replayed commits, syntax/tests, rollback refs, and clean workspace/build mirrors after promotion.
- Treat any unknown Docker volume or writable-layer DB/state file as a rollout blocker.
- Treat `./backups` resolving anywhere outside `/var/lib/docker-runtime/backups` as a rollout blocker; upgrade-v2 SQLite backups are multi-GB and must not land on `/home`/`/`.
- **Never include `tailscale` in automated pull/recreate/apply.** The stack services share the Tailscale sidecar network namespace, so restarting or recreating `hermes-webui-stack-tailscale` drops network access for the WebUI/gateway/Helm/Hindsight containers and can kill the process driving the work.

## Known process breakers to track

These are the failure modes that can bring down the operator path or turn a routine upgrade into an outage:

| Breaker | Why it matters | Required handling |
|---|---|---|
| Tailscale sidecar restart/recreate | Every stack service uses the sidecar network namespace. Recreating it cuts network access for dependent containers, including the WebUI session used to operate Hermes. | Manual-only maintenance from host-local/out-of-band access. Do not include `tailscale` in automated upgrade service lists. |
| Recreating WebUI/gateway from the WebUI session | The session driving the upgrade may disappear mid-command. | Run `apply` from host-local MCP/systemd/tmux and expect WebUI disconnects; preflight/report can run from Hermes. |
| Hindsight restart during generic rollout | Hindsight owns pg0 state on a dedicated LV and backs memory calls. | Keep `hindsight` out of normal web/gateway rollouts; require fresh `/var/lib/hindsight-pg0-backup/last-success.json` before any rollout. |
| Source drift/rebase failures | Private Hermes source can be ahead and behind upstream simultaneously. Historically failures surfaced late during build. | Run `reconcile-source`: skip known checkpoint commits, replay semantic commits in an isolated worktree, verify in the Hermes runtime image, promote cleanly, or delegate to the developer profile with artifacts. |
| `local/...:latest` image semantics | Local images are built, not pulled; `docker compose pull` is misleading/failing for them. | Pull only remote base images; build immutable candidate tags; retag `:latest` only during confirmed apply. |
| Hermes Agent gateway base image policy | The gateway is intentionally based on `nousresearch/hermes-agent:main`, not `:latest`, so the stack tracks upstream main after source reconciliation rather than waiting for versioned image releases. | Keep `Dockerfile.gateway` and `scripts/stack_upgrade_v2.py` `GATEWAY_PULL_REF` aligned on `nousresearch/hermes-agent:main`; regression tests enforce this. |
| Unpersisted container writable-layer DB/state | Recreate loses files outside bind/LV-backed storage. | Preflight blocks on unexpected Docker volumes or DB/state-like writable-layer findings. |
| Host routing ambiguity | M1 host-tools/Docker access and M800 SSH routes have changed over time. | Preflight/report should state the host and Docker reachability; do not assume the old ancillary M800 cron is authoritative. |

## Hermes Agent gateway base image policy

The gateway base-image lane tracks upstream main:

- `Dockerfile.gateway` starts from `nousresearch/hermes-agent:main`.
- `scripts/stack_upgrade_v2.py` pulls `nousresearch/hermes-agent:main` before candidate builds.
- The legacy/manual `scripts/safe_update_stack.py` fallback is kept aligned on the same `:main` pull ref to avoid accidental release-track rebuilds.
- `Dockerfile.gateway` explicitly installs `/usr/bin/tini` because compose entrypoints for Helm/AgentHub/gateways invoke it and upstream `:main` does not currently include it.
- Candidate builds run a runtime dependency smoke check before apply so a missing compose-entrypoint binary blocks before live containers are recreated.
- Local rollout tags such as `local/hermes-webui-stack-gateway:latest` remain local compose/runtime tags; they are still retagged only during confirmed `apply`.

This is a deliberate policy change from `nousresearch/hermes-agent:latest`: `:latest` follows published releases, while `:main` follows the main-branch image. The stack still performs source reconciliation and preflight verification before any build/apply, and unattended apply remains confirmation-gated through the host-side wrapper.

## Durable storage policy checked by preflight

Known durable storage:

- `./hermes-home` — Hermes shared home; bind-mounted into WebUI and gateway profiles.
  Generated profile gateways use the WebUI-compatible runtime layout (`HERMES_HOME=/home/hermeswebui/.hermes/profiles/<profile>`, `HOME=.../home`); see `docs/profile-gateway-runtime-layout.md`. `/opt/data` is a temporary compatibility alias for profile gateways and remains the root/default gateway layout until its separate config-selection issue is resolved.
- `./workspace` — stack workspace; bind-mounted into gateway/WebUI/Helm.
- `./workspace/helm/var/helm.db` — Helm SQLite DB under the workspace bind mount.
- `./workspace/AgentHub/*.db` — AgentHub state/mailbox DBs under the workspace bind mount.
- `./hindsight/data` — Hindsight bind-mounted data/config tree.
- `/srv/hindsight-pg0` — dedicated LV mount for Hindsight pg0.
- `./tailscale-state` and `./config` — Tailscale and serve config.

Skill libraries under `./hermes-home/**/skills` are durable profile data. They are not refreshed by the container upgrade lane. See `docs/skill-maintenance-lane.md` for the separate host-side skill maintenance design, including report/plan/apply modes, profile-scoped backups, and the no-auto-install-new-skills policy.

The one observed Docker anonymous volume is Helm `/opt/data`, inherited from the base image. It is explicitly allowlisted only because Helm runtime env points `HERMES_HOME` and `HELM_DB_PATH` to bind-mounted paths. Any other Docker volume blocks rollout.

Hindsight pg0 is not copied by the upgrade script. Instead, preflight requires a fresh successful marker from the existing LV snapshot export:

- marker: `/var/lib/hindsight-pg0-backup/last-success.json`
- expected source: `/dev/ubuntu-vg/hindsight-pg0`
- default max age: 36 hours


## Upgrade backup storage guardrails

`preflight`, `backup-sqlite`, and `apply` now verify the upgrade backup root before any SQLite backup is written:

- `./backups` must resolve under `/var/lib/docker-runtime/backups` by default. Override only for a deliberate storage migration with `STACK_UPGRADE_EXPECTED_BACKUP_STORAGE_ROOT`.
- `/` must have at least 20 GiB free by default. Override only for an emergency with `STACK_UPGRADE_MIN_ROOT_FREE_BYTES`.
- `backup-sqlite` refuses to continue on backup-storage/root-space blockers even when `--allow-preflight-failures` is used.
- After a successful SQLite backup, timestamped `backups/upgrade-v2/<timestamp>/` sets are pruned to the newest 5 by default. Override with `--backup-retention-keep` or `STACK_UPGRADE_BACKUP_RETENTION_KEEP`.

Manual dry-run and confirmed prune:

```bash
./scripts/stack_upgrade_v2.py prune-backups --json --keep 5
./scripts/stack_upgrade_v2.py prune-backups --json --keep 5 --confirm PRUNE-UPGRADE-BACKUPS
```

This retention command only targets timestamp-named upgrade-v2 backup-set directories (`YYYYmmddTHHMMSSZ`). It intentionally ignores unrelated backup folders.

## Commands

Run from the stack directory on `josh-m1`:

```bash
cd ~/docker-compose/hermes-webui-stack
```

Read-only preflight:

```bash
./scripts/stack_upgrade_v2.py preflight --json
```

Refresh upstream refs before source gate:

```bash
./scripts/stack_upgrade_v2.py preflight --fetch --json
```

Autonomous source reconciliation (does not change containers):

```bash
./scripts/stack_upgrade_v2.py reconcile-source --fetch --promote --json
```

This stage creates a disposable worktree under `state/upgrade-v2/source-reconcile/`, starts from `upstream/main`, skips known safe-update checkpoint commits, cherry-picks semantic private commits, and verifies with:

- syntax compilation over Python files inside `local/hermes-webui-stack-gateway:latest`;
- changed Python tests with `pytest -q -p no:cacheprovider -o addopts=""`.

If replay or verification fails and `--delegate-on-failure` is enabled (default), it writes a context artifact and submits an AgentHub verified-developer task from `infrastructure` to `developer`. That is not a completed upgrade; it is an autonomous escalation with enough context for a developer agent to resolve the merge without user input.

Emergency DB capture only (does not change containers):

```bash
./scripts/stack_upgrade_v2.py backup-sqlite --allow-preflight-failures --json
```

Normal DB backup before candidate work (requires preflight pass):

```bash
./scripts/stack_upgrade_v2.py backup-sqlite --fetch --json
# optional: ./scripts/stack_upgrade_v2.py backup-sqlite --fetch --json --backup-retention-keep 5
```

Candidate build only (requires preflight pass; does not recreate containers). A successful build writes an immutable green manifest under `state/upgrade-v2/candidates/<id>/manifest.json` and refreshes `state/upgrade-v2/candidates/latest-green-candidate.json`:

```bash
./scripts/stack_upgrade_v2.py build-candidate --fetch --json
```

Confirmed rollout of the latest green frozen candidate. This does **not** fetch upstream or rebuild; it reads the manifest, verifies candidate image IDs still match, takes backups, retags candidate images to `:latest`, recreates the allowed services, verifies, and rolls back if needed:

```bash
./scripts/stack_upgrade_v2.py apply-candidate --json --confirm RECREATE-HERMES-STACK
# optional: ./scripts/stack_upgrade_v2.py apply-candidate --json --confirm RECREATE-HERMES-STACK --manifest state/upgrade-v2/candidates/<id>/manifest.json
```

Legacy all-in-one rollout remains available for manual recovery only, but should not be the scheduled path because it can rediscover source/build drift during rollout:

```bash
./scripts/stack_upgrade_v2.py apply --json --confirm RECREATE-HERMES-STACK
```

## Current source reconciliation state

The original installation blocker (`workspace_source_behind_upstream` plus `build_source_mirror_mismatch`) has been resolved by `reconcile-source --fetch --promote`. The process skipped the known safe-update checkpoint commit, replayed five semantic commits, verified syntax and targeted tests, and promoted the same reconciled head into both workspace and build mirrors. Future source drift should be handled by the same command before build/apply.

## Automation guidance

The replacement for the old updater is **host-side autonomous candidate validation**, not an in-container Hermes/gateway cron and not unattended live apply. The active unattended path is:

```bash
/home/jmelchiori/docker-compose/hermes-webui-stack/scripts/hermes-autonomous-upgrade.sh
```

With no arguments, that wrapper runs outside the stack containers and calls:

```bash
./scripts/stack_upgrade_v2.py build-candidate --fetch --json --tag-suffix <run-id>
```

This default scheduled path fetches/reconciles, builds candidate images, runs smoke checks, and writes a frozen green candidate manifest. It does **not** retag `:latest` and does **not** recreate WebUI/gateway/Helm/dashboard containers.

Live rollout is explicit only:

```bash
./scripts/hermes-autonomous-upgrade.sh --apply
```

`--apply` calls `apply-candidate`, which uses the latest green manifest without fetching/rebuilding. This prevents the old “discover one blocker per day” loop where a fixed reconcile could be invalidated by a newer upstream before apply.

Do **not** schedule live `apply` as a Hermes cron inside the gateway/WebUI runtime. Recreating gateway/WebUI can kill the very process driving the upgrade. Schedule only candidate validation unattended; run `--apply` from host control when a rollout is desired.

Current automation status:

- `scripts/hermes-autonomous-upgrade.sh` is the host-side unattended candidate-validation entry point; `--apply` is explicit live rollout.
- `scripts/nightly-stack-update.sh` delegates Hermes handling to the autonomous wrapper; Plex handling remains separate.
- The automated recreate set is limited to `hermes-webui`, `helm`, `hermes-gateway`, and `hermes-gateway-*`.
- `tailscale` and `hindsight` are manual-only for normal upgrades.
- If source reconciliation returns `delegated_source_reconcile`, candidate validation stops before build and reports the delegation/context artifact.
- Reports are written to `state/upgrade-v2/last-autonomous-upgrade.json` and `logs/hermes-autonomous-upgrade.log`.
- SQLite backups and backup retention pruning happen during explicit `apply-candidate`, not during candidate-only scheduled validation.

Installed scheduler:

- Systemd service: `hermes-webui-stack-autonomous-upgrade.service`
- Systemd timer: `hermes-webui-stack-autonomous-upgrade.timer`
- Schedule: `*-*-* 04:20:00 America/Chicago` with `RandomizedDelaySec=10m`
- Installed unit paths: `/etc/systemd/system/hermes-webui-stack-autonomous-upgrade.{service,timer}`
- Source unit files: `systemd/hermes-webui-stack-autonomous-upgrade.{service,timer}`

Verification commands:

```bash
systemctl list-timers --all hermes-webui-stack-autonomous-upgrade.timer
systemctl status hermes-webui-stack-autonomous-upgrade.timer --no-pager
journalctl -u hermes-webui-stack-autonomous-upgrade.service -n 100 --no-pager
./scripts/hermes-autonomous-upgrade.sh          # candidate-only; no recreate
./scripts/hermes-autonomous-upgrade.sh --dry-run # reconcile/preflight only; no build/recreate
```

## Files

- `scripts/stack_upgrade_v2.py` — host-side staged upgrade workflow.
- `scripts/hermes-autonomous-upgrade.sh` — unattended host-side candidate-validation wrapper; explicit `--apply` rollout wrapper.
- `systemd/hermes-webui-stack-autonomous-upgrade.service` — systemd oneshot service source.
- `systemd/hermes-webui-stack-autonomous-upgrade.timer` — systemd daily timer source.
- `state/upgrade-v2/last-*.json` — most recent stage reports.
- `state/upgrade-v2/last-autonomous-upgrade.json` — most recent autonomous wrapper report.
- `state/upgrade-v2/candidates/latest-green-candidate.json` — latest green frozen candidate manifest for explicit `apply-candidate`.
- `backups/upgrade-v2/<timestamp>/` — SQLite backup manifests and DB copies; retained newest 5 by default.
