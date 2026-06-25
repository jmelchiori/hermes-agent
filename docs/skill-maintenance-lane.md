# Hermes Skill Maintenance Lane

This document defines a separate maintenance lane for Hermes skills in `hermes-webui-stack`. It is intentionally separate from the container/image upgrade lane because skills change agent behavior and prompt surface area, even when no containers restart.

## Short version

Build the lane as a host-owned, out-of-band systemd process with three modes:

1. `report` — read-only inventory/check/audit summary across selected profiles.
2. `plan` — fetch candidate updates, snapshot current state, scan/diff candidates, and write an apply plan without mutating skills.
3. `apply` — update only explicitly allowed hub-installed skills after snapshot, scan, and rollback preparation.

Default policy:

- Auto-report all profiles in scope.
- Do **not** auto-install new skills.
- Do **not** mutate local/profile-authored skills.
- Do **not** update community/tap skills unattended.
- Start with manual apply only; later allow unattended apply for `official` hub skills that pass guard/audit and are on an allowlist.
- Reuse the stack maintenance lock so skill maintenance never overlaps container rollout.

## Why this is not part of the container upgrade lane

The container upgrade lane manages source reconciliation, image builds, service recreation, endpoint verification, and rollback tags. Skill maintenance is different:

- Skills are persisted profile data under `hermes-home`, not normal image contents.
- Updating a skill changes future system prompts and operator behavior.
- Existing sessions may keep cached prompts until reset/new session, so effect timing differs from service restart timing.
- `hermes skills update` currently force-reinstalls hub-installed skills; the stack needs stronger snapshot/diff/rollback semantics around that.
- New skill discovery is a curation problem, not an infrastructure rollout problem.

## Verified current behavior

Observed from the live gateway container and stack source:

- `hermes skills check`, `hermes skills update`, `hermes skills audit`, and `hermes skills snapshot export|import` are available in the gateway CLI.
- Container upgrade scripts do not call `hermes skills ...`.
- `hermes skills check` only checks hub-installed skills tracked by `skills/.hub/lock.json`; local and bundled skills are classified separately.
- `hermes skills update [name]` filters `check_for_skill_updates()` for `status == update_available`, then calls `do_install(..., force=True)`.
- The install path removes the existing installed skill directory before moving the scanned quarantine copy into place.
- The hub installer records provenance in `skills/.hub/lock.json` and appends to the hub audit log.
- Bundled-skill sync logic has its own user-modified guard: if the on-disk copy differs from the manifest's origin hash, sync reports `user_modified` and skips overwriting it.
- `hermes skills reset <name>` can clear bundled-skill tracking, and `hermes skills reset <name> --restore --yes` can replace the local copy with the current bundled version. Treat this as destructive and never run it unattended.
- Hub-installed skill update discovery compares the hub lockfile's stored `content_hash` to the fetched upstream bundle hash. It does **not** by itself prove the on-disk hub skill still matches the lockfile hash. The maintenance lane must compute live on-disk hashes before update so local edits to hub-installed skills are not clobbered by `hermes skills update`.
- A live check against sampled homes/profiles reported two hub updates available: `docker-management` and `sherlock`.

Implication: use the CLI/module behavior, but wrap it with stack-level snapshots, policy, and reports before applying anything unattended.

## Scope

### Initial profile scope

Inventory can cover every profile directory, but apply should start with a conservative allowlist:

```text
/opt/data
/opt/data/profiles/developer
/opt/data/profiles/personal
/opt/data/profiles/infrastructure
```

Recommended policy:

- `report`: discover all profiles under `/opt/data/profiles/*` plus `/opt/data` and report them.
- `plan`: same as `report`, but mark profiles not in the apply allowlist as `report_only`.
- `apply`: mutate only profiles in `apply_profiles`.

### Skill source scope

| Source/type | Report | Plan | Unattended apply | Notes |
|---|---:|---:|---:|---|
| hub `official` | yes | yes | eventually, allowlisted only | Safest candidate class; still behavior-changing. |
| hub `trusted` | yes | yes | no initially | Require reviewed apply until enough history. |
| hub `community` / taps | yes | yes | no | Require manual review; prompt-injection/security risk is higher. |
| hub-installed but locally modified | yes | diff/review only | no | Detect by recomputing on-disk hash and comparing with `skills/.hub/lock.json`; do not call `skills update` until reviewed. |
| local skills | inventory + validation | review queue only | no | These are our authored operational memory; patch via skill authoring/curation, not bulk updates. |
| bundled skills, unmodified | yes | optional later | no initially | Bundled sync can update safely only when user copy still matches origin hash. |
| bundled skills, `user_modified` | yes | diff/review only | no | Preserve local edits; never run `skills reset --restore` unattended. |
| name collisions with newly bundled skills | yes | review queue only | no | Upstream bundled skill shipped with a name we already use locally; keep ours unless explicitly curated. |
| new skill discovery | report only | curation queue only | never | New skills should become explicit review tasks, not auto-installs. |

## Lane modes

### `report` mode

Read-only. Safe to run daily.

For each profile/home:

1. Run hub update discovery via module API or CLI:
   - preferred: Python imports `tools.skills_hub.check_for_skill_updates()` and emits JSON without Rich table parsing;
   - fallback: `hermes skills check` and parse table only for human-readable report.
2. Run `hermes skills list --source hub` and `hermes skills list --enabled-only`.
3. Run `hermes skills audit` for hub-installed skills, or at least for update candidates.
4. Record:
   - profile/home path;
   - installed hub skills from `skills/.hub/lock.json`;
   - update candidates with `current_hash` and `latest_hash`;
   - audit verdicts;
   - disabled/enabled state summary;
   - any unreadable/invalid skill trees.
5. Write report to:

```text
state/skill-maintenance/last-report.json
state/skill-maintenance/reports/<timestamp>.json
logs/hermes-skill-maintenance.log
```

No user-visible alert for no-op reports unless configured. Send/notify only on updates available, audit failures, invalid manifests, or script errors.

### Modified skill handling

Modified skills are first-class maintenance findings, not errors and not automatic overwrite targets.

The lane must distinguish at least four cases:

1. **Bundled skill, unmodified** — present in `.bundled_manifest`, on-disk hash equals recorded origin hash. It may be eligible for a future bundled-sync lane, but not part of initial hub-update apply.
2. **Bundled skill, user-modified** — present in `.bundled_manifest`, on-disk hash differs from origin hash. Preserve it, report it, and optionally diff it against both recorded origin and current bundled source. Do not run `hermes skills reset --restore` unless a human/curator explicitly chooses to discard local edits.
3. **Hub-installed skill, unmodified since install** — present in `skills/.hub/lock.json`, on-disk hash equals lockfile `content_hash`. This is the only class eligible for `hermes skills update <name>` after source/trust/allowlist checks.
4. **Hub-installed skill, locally modified since install** — present in `skills/.hub/lock.json`, on-disk hash differs from lockfile `content_hash`. Preserve it and classify as `manual_review`; a raw `hermes skills update <name>` would replace the local tree and can lose edits.

For modified skills, the plan artifact should include:

- profile/home path;
- skill name and category/path;
- provenance (`local`, `bundled`, `hub official/trusted/community/tap`);
- baseline hash if known;
- live on-disk hash;
- upstream/candidate hash if available;
- file-level diff paths;
- recommendation: keep local, rebase local edits onto upstream, reset to upstream, promote local edits into source/bundled repo, or archive/delete.

Curation can modify skills, but that is a separate reviewed action from unattended update apply. Use `skill_manage` for the active Hermes profile when possible; use direct file edits under the target `hermes-home/profiles/<profile>/skills/...` when maintaining stack profile skills that are outside the current tool profile.


### `plan` mode

No mutation of live skills. Safe to run before manual review.

For each update candidate:

1. Create a filesystem snapshot/tarball of the profile's `skills/` tree and `.hub` metadata.
2. Export a logical hub snapshot:

```bash
HERMES_HOME=<home> /opt/hermes/.venv/bin/hermes skills snapshot export <artifact>/hub-snapshot.json
```

3. Fetch candidate bundle using Hermes module APIs.
4. Run the same guard/audit scan used by the installer.
5. Create a candidate diff:
   - old installed files vs fetched bundle;
   - `SKILL.md` frontmatter changes;
   - added/removed files;
   - threat/audit findings.
6. Classify candidate:

```text
auto_apply_ok     official, allowlisted, guard safe/caution per policy, diff size below threshold
manual_review     trusted/community/tap, non-allowlisted, large diff, new files/scripts/assets
blocked           dangerous verdict, fetch failure, invalid SKILL.md, path validation failure
```

7. Write an apply plan:

```text
state/skill-maintenance/last-plan.json
state/skill-maintenance/plans/<timestamp>.json
state/skill-maintenance/artifacts/<timestamp>/<profile>/<skill>/diff.patch
```

### `apply` mode

Mutates profile skill state. Should require explicit confirmation initially:

```bash
./scripts/hermes-skill-maintenance.sh apply --confirm UPDATE-HERMES-SKILLS
```

Apply procedure per profile:

1. Acquire the stack maintenance lock. Reuse the existing autonomous-upgrade lock or adopt a shared stack lock so this lane cannot overlap container rollout:

```text
/tmp/hermes-webui-stack-autonomous-upgrade.lock
```

2. Create rollback artifacts before first mutation:

```text
backups/skill-maintenance/<timestamp>/<profile_slug>/skills.tar.zst
backups/skill-maintenance/<timestamp>/<profile_slug>/hub-snapshot.json
backups/skill-maintenance/<timestamp>/<profile_slug>/pre-state.json
```

3. Re-run update discovery and live on-disk hash checks immediately before apply; abort if candidates differ from the reviewed plan unless `--refresh-plan` is explicit.
4. Refuse to update any hub skill whose live on-disk hash differs from the lockfile `content_hash`, unless an explicit curation plan says to overwrite or rebase it.
5. For each allowed candidate:
   - run `hermes skills audit <name>` on current install;
   - run candidate guard/scan;
   - call `hermes skills update <name>` or module-level install;
   - run `hermes skills audit <name>` after update;
   - verify `skills/.hub/lock.json` latest hash matches expected candidate hash;
   - verify `SKILL.md` exists and frontmatter parses.
6. If any update fails verification, restore that profile from its backup and mark the profile `rolled_back`.
7. Write final report:

```text
state/skill-maintenance/last-apply.json
state/skill-maintenance/applies/<timestamp>.json
```

8. Do not restart containers. Existing sessions may retain prompt-cache state; updates take effect on new/reset sessions.

## Rollback

Rollback should be profile-scoped and artifact-driven:

```bash
./scripts/hermes-skill-maintenance.sh rollback --backup-id <timestamp> --profile developer --confirm RESTORE-HERMES-SKILLS
```

Rollback restores:

- the entire `skills/` tree for the target profile/home;
- `skills/.hub/lock.json`, taps, audit log if included in the tree;
- optional hub snapshot metadata for provenance comparison.

Post-rollback verification:

- `hermes skills list --source hub` succeeds;
- `hermes skills check` succeeds;
- `skills/.hub/lock.json` is valid JSON;
- all enabled skills in the profile still have a `SKILL.md`.

## New skill discovery policy

Do not auto-install new skills as part of this lane.

Recommended discovery behavior:

1. Weekly report of official/trusted new skills relevant to enabled toolsets/profile roles.
2. Put candidates into a curation queue with:
   - source identifier;
   - trust/source;
   - description;
   - security scan summary;
   - why it may be useful for this stack/profile;
   - recommendation: install, absorb into local umbrella skill, ignore.
3. Manual review decides one of:
   - install into a specific profile;
   - adapt/merge ideas into a local skill;
   - reject.

This avoids silently expanding the prompt surface area and keeps Josh's operational skills curated rather than turning the stack into a bulk skill importer.

## Decision delivery mechanism

Skill-related decisions should not live only in cron logs or transient chat. The lane uses a three-layer delivery model:

1. **Machine-readable decision queue** — canonical state for automation.
2. **Human-readable vault note** — durable review surface and historical rationale.
3. **Discord summary** — concise notification when attention or accountability is needed.

### Decision record

Every non-noop finding that requires a decision becomes a decision record with a stable ID:

```text
skilldec-<YYYYMMDD>-<profile_slug>-<skill_name>-<short_hash>
```

Stored as:

```text
state/skill-maintenance/decisions/<decision_id>.json
state/skill-maintenance/decision-queue.jsonl
```

Minimum schema:

```json
{
  "id": "skilldec-20260522-developer-docker-management-a1b2c3",
  "created_at": "2026-05-22T05:00:00Z",
  "profile": "developer",
  "home": "/opt/data/profiles/developer",
  "skill": "docker-management",
  "path": "devops/docker-management",
  "provenance": "hub:official",
  "finding": "hub_locally_modified_and_upstream_update_available",
  "classification": "manual_review",
  "recommended_decision": "rebase_local_edits_onto_upstream",
  "allowed_actions": ["keep_local", "rebase", "reset_to_upstream", "promote_local", "archive"],
  "risk": "medium",
  "rationale": "Live on-disk hash differs from lockfile and upstream has an update; raw `hermes skills update` would clobber local edits.",
  "artifacts": {
    "diff": "state/skill-maintenance/artifacts/20260522/developer/docker-management/diff.patch",
    "candidate_summary": "state/skill-maintenance/artifacts/20260522/developer/docker-management/summary.md",
    "backup": "backups/skill-maintenance/20260522T050000Z/developer/skills.tar.zst"
  },
  "status": "open",
  "owner": "developer",
  "delivery": ["vault", "discord:#friday"],
  "applied_by": null,
  "applied_at": null
}
```

### Classifications and delivery

| Classification | Meaning | Delivery | Default action |
|---|---|---|---|
| `auto_apply_ok` | Policy says this can be applied unattended after backups and verification. | Include in report/vault; Discord only if applied or failed. | Apply in scheduled apply lane once enabled. |
| `manual_review` | Needs curation, but not urgent. | Vault note + Discord digest. | No mutation; keep open in queue. |
| `blocked` | Unsafe or impossible to apply without intervention. | Vault note + Discord alert. | No mutation; optionally delegate to developer/infrastructure. |
| `report_only` | Informational inventory/discovery. | Vault note; Discord only in digest. | No mutation. |
| `auto_reject` | Candidate is clearly not useful or violates policy. | Vault audit trail only. | Mark closed with rationale. |

### Vault delivery

Long-form decisions go into the Obsidian vault so they remain searchable and reviewable:

```text
/workspace/notes/obsidian-vault/skill-maintenance/YYYY-MM-DD.md
/workspace/notes/obsidian-vault/skill-maintenance/open-decisions.md
```

Each entry should include:

- decision ID;
- skill/profile;
- current state;
- recommended decision;
- rationale;
- artifact paths;
- owner;
- next review date or delegation ID.

The vault note is the review surface. The JSON queue is the automation source of truth.

### Discord delivery

Discord is the attention channel, not the canonical state store. Send concise summaries to `discord:#friday` when:

- new `manual_review` or `blocked` decisions appear;
- an unattended apply changed skills;
- an unattended apply failed or rolled back;
- stale open decisions exceed the configured age threshold.

Example Discord summary:

```text
Skill maintenance: 3 decisions need review
- developer/docker-management: upstream update + local edits; recommend rebase (skilldec-...)
- personal/hermes-agent: bundled user-modified; recommend keep local for now
- infrastructure/native-mcp: bundled user-modified; recommend compare against current bundled

Vault: /workspace/notes/obsidian-vault/skill-maintenance/2026-05-22.md
Queue: state/skill-maintenance/decision-queue.jsonl
```

### AgentHub delegation

When a decision has an obvious engineering action but should not be applied blindly, the lane should create an AgentHub delegation instead of waiting for user steering.

Use cases:

- rebase local skill edits onto upstream candidate;
- promote a local profile skill improvement into a source/bundled skill;
- compare an external/new skill and recommend absorb/install/reject;
- fix invalid frontmatter or broken skill structure.

Default ownership:

- `developer` profile owns skill content curation and source/bundled skill edits.
- `infrastructure` profile owns host-side scheduling, backup, apply, and rollback plumbing.

Delegation artifacts should reference the decision ID and include all diff/backup paths. When the delegation completes, update the decision record with status and result artifact.

### Decision lifecycle

```text
open -> delegated -> planned -> applied -> verified -> closed
open -> rejected -> closed
open -> stale -> re-notify or delegate
open -> superseded -> closed
```

Closure requires one of:

- verified apply report;
- explicit rejection rationale;
- superseded-by decision ID;
- delegation result with verified no-op recommendation.

### Default delivery policy

- Daily read-only report writes queue/vault artifacts.
- Discord receives only actionable summaries, not full diffs.
- Unattended apply reports what changed and where the backup lives.
- Modified skills never get overwritten merely because a decision exists; the decision must be `applied` by curation or by an allowlisted policy path.


## Scheduling

### Systemd service identity

The installed report service must run as `jmelchiori` with docker group access, matching the autonomous upgrade service:

```ini
User=jmelchiori
Group=jmelchiori
SupplementaryGroups=docker
Environment=HOME=/home/jmelchiori
```

Do not leave the service as implicit root. The wrapper shares `/tmp/hermes-webui-stack-autonomous-upgrade.lock` with the container-upgrade lane; on Linux hosts with protected regular-file settings, a root-owned service can fail to open the existing user-owned lock with `Permission denied`. Running as `jmelchiori` also preserves Docker socket access semantics for the report probes.


Recommended first rollout:

1. Daily `report` timer, after the container upgrade window:

```text
05:05 America/Chicago + RandomizedDelaySec=10m
```

2. Manual `plan`/`apply` while we validate output quality. Implemented commands:

```bash
./scripts/hermes-skill-maintenance.sh plan
./scripts/hermes-skill-maintenance.sh apply --dry-run --confirm UPDATE-HERMES-SKILLS
./scripts/hermes-skill-maintenance.sh apply --confirm UPDATE-HERMES-SKILLS
./scripts/hermes-skill-maintenance.sh rollback --backup-id <timestamp> --profile <profile> --confirm RESTORE-HERMES-SKILLS
```

`plan` writes `state/skill-maintenance/last-plan.json` and creates verified profile backups under `backups/skill-maintenance/<timestamp>/<profile>/skills.tar.zst`. `apply --dry-run` validates confirmation, plan status, backup readability, and live hash stability without mutating skills. Non-dry-run `apply` is intentionally confirmation-gated and creates a fresh apply-time backup before running `hermes skills update <name>`.

3. After several clean cycles, optional weekly unattended apply for allowlisted official updates only:

```text
Sunday 05:25 America/Chicago + RandomizedDelaySec=20m
```

Separate systemd units/timers should be used; do not schedule this as a Hermes cron running inside the gateway.

## Proposed files

```text
config/skill-maintenance.yaml
scripts/skill_maintenance_lane.py
scripts/hermes-skill-maintenance.sh
systemd/hermes-webui-stack-skill-maintenance-report.service
systemd/hermes-webui-stack-skill-maintenance-report.timer
systemd/hermes-webui-stack-skill-maintenance-apply.service   # optional later
systemd/hermes-webui-stack-skill-maintenance-apply.timer     # optional later
state/skill-maintenance/
backups/skill-maintenance/
logs/hermes-skill-maintenance.log
```

## Configuration sketch

```yaml
profiles:
  discover: true
  include_homes:
    - /opt/data
    - /opt/data/profiles/developer
    - /opt/data/profiles/personal
    - /opt/data/profiles/infrastructure
  apply_homes:
    - /opt/data/profiles/developer
    - /opt/data/profiles/personal
    - /opt/data/profiles/infrastructure

policy:
  new_skill_discovery: report_only
  local_skills: inventory_only
  bundled_skills: inventory_only
  hub_sources:
    official:
      report: true
      plan: true
      auto_apply: false   # flip later only after manual cycles
      allowlist:
        - docker-management
        - sherlock
    trusted:
      report: true
      plan: true
      auto_apply: false
    community:
      report: true
      plan: true
      auto_apply: false
  block_if:
    - dangerous_scan_verdict
    - invalid_frontmatter
    - path_validation_failure
    - missing_skill_md
    - update_candidate_changed_after_plan
    - live_hub_skill_hash_differs_from_lockfile
    - bundled_skill_user_modified_without_curation_plan

limits:
  max_updates_per_apply: 5
  max_diff_lines_auto_apply: 400
  require_confirm_for_apply: true

runtime:
  gateway_container: hermes-webui-stack-gateway
  hermes_bin: /opt/hermes/.venv/bin/hermes
  stack_lock: /tmp/hermes-webui-stack-autonomous-upgrade.lock
```

## Implementation notes

### Prefer module JSON over Rich-table parsing

Inside the gateway container, use Hermes modules to produce structured JSON for checks:

```python
from tools.skills_hub import check_for_skill_updates
rows = check_for_skill_updates()
for row in rows:
    row.pop("bundle", None)  # not JSON serializable and too large for reports
```

Run this with each target `HERMES_HOME` so paths resolve to the correct profile.

### Use container exec from host scripts

The systemd unit should run on the host, then call into the gateway container for Hermes CLI/module operations. That keeps scheduling resilient while still using the exact Hermes runtime and dependencies that own the profile homes.

### Avoid prompt-cache invalidation side effects

Do not use `--now`/cache-invalidation behavior during unattended maintenance. Let changes apply to future/new sessions. If a particular profile needs immediate adoption, that is a manual operator action.

### Keep reports concise

Reports should include candidate names, source/trust, hashes, verdict, and artifact paths. Large diffs should live on disk, not in chat/notifications.

## Acceptance criteria for first implementation

- `report` mode runs read-only and produces JSON across selected profiles.
- `report` mode detects bundled `user_modified` skills and hub-installed skills whose live on-disk hash differs from the hub lockfile hash.
- `plan` mode creates rollback artifacts and candidate diffs without modifying live `skills/` directories.
- `apply` mode refuses to run without confirmation.
- `apply` mode can update one named official skill in one profile and verify post-state.
- A failed apply restores that profile from backup.
- The skill lane cannot overlap the autonomous container upgrade lane.
- No new skills are auto-installed.
- No local/profile-authored skills are overwritten.
- No modified bundled or modified hub-installed skill is overwritten without an explicit curation plan.
- Reports clearly distinguish `report_only`, `manual_review`, `auto_apply_ok`, and `blocked` candidates.
