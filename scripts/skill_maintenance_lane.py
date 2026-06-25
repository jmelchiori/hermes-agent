#!/usr/bin/env python3
"""Host-side Hermes skill maintenance lane.

Report mode inspects live profile skill trees and fetches upstream hub hashes
through an isolated temporary HERMES_HOME inside the gateway container. It must
not mutate live skills or live hub metadata.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STACK_DIR = Path(__file__).resolve().parents[1]
HOST_HERMES_HOME = STACK_DIR / "hermes-home"
STATE_DIR = STACK_DIR / "state" / "skill-maintenance"
REPORT_DIR = STATE_DIR / "reports"
PLAN_DIR = STATE_DIR / "plans"
APPLY_DIR = STATE_DIR / "applies"
ROLLBACK_DIR = STATE_DIR / "rollbacks"
ARTIFACT_DIR = STATE_DIR / "artifacts"
DECISION_DIR = STATE_DIR / "decisions"
LOG_DIR = STACK_DIR / "logs"
BACKUP_DIR = STACK_DIR / "backups" / "skill-maintenance"
VAULT_DIR = STACK_DIR / "workspace" / "notes" / "obsidian-vault" / "skill-maintenance"
GATEWAY_CONTAINER = "hermes-webui-stack-gateway"
GATEWAY_PYTHON = "/opt/hermes/.venv/bin/python"
GATEWAY_PYTHONPATH = ":".join([
    "/workspace/hermes-agent-private",
    "/workspace/hermes-agent-private/build/lib",
    "/opt/hermes/.venv/lib/python3.13/site-packages",
    "/workspace/AgentHub",
])
DISCORD_TARGET = "discord:#friday"
APPLY_ALLOWLIST = {"docker-management", "sherlock"}
APPLY_PROFILE_NAMES = {"developer", "personal", "infrastructure"}
DECISION_CLASSIFICATIONS = {"manual_review", "blocked", "auto_apply_ok"}


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    home: Path
    apply_allowed: bool = False
    container_home: str | None = None

    @property
    def display_home(self) -> str:
        return self.container_home or str(self.home)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._").lower()
    return slug or "unknown"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        return {"__error__": f"invalid_json:{exc}"}
    except OSError as exc:
        return {"__error__": f"unreadable:{exc}"}


def hub_content_hash(skill_path: Path) -> str:
    """Match tools.skills_guard.content_hash for live hub-installed skills."""
    hasher = hashlib.sha256()
    if skill_path.is_dir():
        for fpath in sorted(skill_path.rglob("*")):
            if fpath.is_file():
                try:
                    hasher.update(fpath.read_bytes())
                except OSError:
                    continue
    elif skill_path.is_file():
        hasher.update(skill_path.read_bytes())
    return f"sha256:{hasher.hexdigest()[:16]}"


def bundled_dir_hash(directory: Path) -> str:
    """Match tools.skills_sync._dir_hash for bundled manifest comparisons."""
    hasher = hashlib.md5()
    try:
        for fpath in sorted(directory.rglob("*")):
            if fpath.is_file():
                rel = fpath.relative_to(directory)
                hasher.update(str(rel).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    except (OSError, IOError):
        pass
    return hasher.hexdigest()


def parse_skill_name(skill_md: Path) -> str | None:
    try:
        lines = skill_md.read_text(errors="replace").splitlines()
    except OSError:
        return None
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    search_lines = lines[1:] if in_frontmatter else lines[:25]
    for line in search_lines:
        stripped = line.strip()
        if in_frontmatter and stripped == "---":
            break
        if stripped.startswith("name:"):
            return stripped.split(":", 1)[1].strip().strip('"\'') or None
    return None


def iter_skill_dirs(skills_dir: Path) -> list[Path]:
    if not skills_dir.exists():
        return []
    result: list[Path] = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        if ".hub" in skill_md.parts:
            continue
        result.append(skill_md.parent)
    return sorted(result)


def find_skill_dir(skills_dir: Path, skill_name: str) -> Path | None:
    direct_matches = [p for p in iter_skill_dirs(skills_dir) if p.name == skill_name]
    if len(direct_matches) == 1:
        return direct_matches[0]
    named_matches = [p for p in iter_skill_dirs(skills_dir) if parse_skill_name(p / "SKILL.md") == skill_name]
    if len(named_matches) == 1:
        return named_matches[0]
    return direct_matches[0] if direct_matches else (named_matches[0] if named_matches else None)


def discover_profiles() -> list[ProfileSpec]:
    specs: list[ProfileSpec] = []
    if HOST_HERMES_HOME.exists():
        specs.append(ProfileSpec("default", HOST_HERMES_HOME, False, "/opt/data"))
    profiles_root = HOST_HERMES_HOME / "profiles"
    if profiles_root.exists():
        for home in sorted(p for p in profiles_root.iterdir() if p.is_dir()):
            name = safe_slug(home.name)
            specs.append(ProfileSpec(
                name=name,
                home=home,
                apply_allowed=name in APPLY_PROFILE_NAMES,
                container_home=f"/opt/data/profiles/{home.name}",
            ))
    order = {"default": 0, "developer": 1, "personal": 2, "infrastructure": 3}
    return sorted(specs, key=lambda p: (order.get(p.name, 99), p.name))


def load_hub_lock(skills_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    lock_path = skills_dir / ".hub" / "lock.json"
    data = read_json(lock_path, {"version": 1, "installed": {}})
    if isinstance(data, dict) and "__error__" in data:
        errors.append({"code": "invalid_hub_lock", "path": str(lock_path), "message": data["__error__"]})
        return {"version": 1, "installed": {}}, errors
    if not isinstance(data, dict) or not isinstance(data.get("installed", {}), dict):
        errors.append({"code": "invalid_hub_lock_shape", "path": str(lock_path), "message": "expected object with installed map"})
        return {"version": 1, "installed": {}}, errors
    return data, errors


def load_bundled_manifest(skills_dir: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    manifest_path = skills_dir / ".bundled_manifest"
    if not manifest_path.exists():
        return {}, errors
    try:
        raw = manifest_path.read_text()
    except OSError as exc:
        errors.append({"code": "invalid_bundled_manifest", "path": str(manifest_path), "message": f"unreadable:{exc}"})
        return {}, errors

    stripped = raw.strip()
    if not stripped:
        return {}, errors
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append({"code": "invalid_bundled_manifest", "path": str(manifest_path), "message": f"invalid_json:{exc}"})
            return {}, errors
        if not isinstance(data, dict):
            errors.append({"code": "invalid_bundled_manifest_shape", "path": str(manifest_path), "message": "expected name-to-hash map"})
            return {}, errors
        return {str(k): str(v) for k, v in data.items()}, errors

    manifest: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            errors.append({
                "code": "invalid_bundled_manifest_line",
                "path": str(manifest_path),
                "line": line_number,
                "message": "expected name:hash",
            })
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            errors.append({
                "code": "invalid_bundled_manifest_line",
                "path": str(manifest_path),
                "line": line_number,
                "message": "empty name or hash",
            })
            continue
        manifest[name] = value
    return manifest, errors


def classify_hub_update(name: str, entry: dict[str, Any], spec: ProfileSpec) -> tuple[str, str, str, str]:
    source = entry.get("source", "")
    scan_verdict = entry.get("scan_verdict", "")
    if scan_verdict == "dangerous":
        return (
            "blocked",
            "do_not_update_until_guard_verdict_is_reviewed",
            "high",
            "Existing hub lock records scan_verdict=dangerous; apply must not proceed automatically.",
        )
    if source == "official" and name in APPLY_ALLOWLIST and spec.apply_allowed:
        return (
            "auto_apply_ok",
            "update_allowlisted_official_skill",
            "low",
            "Official allowlisted hub skill is unmodified locally; eligible for future guarded apply after backup/plan verification.",
        )
    if not spec.apply_allowed:
        return (
            "report_only",
            "keep_reporting_profile_outside_apply_allowlist",
            "low",
            "Profile is not in the apply allowlist; report the candidate without planning mutation.",
        )
    return (
        "manual_review",
        "review_non_allowlisted_or_non_official_update",
        "medium",
        "Candidate is not both official and allowlisted for unattended apply.",
    )


def inspect_profile(spec: ProfileSpec, update_rows: list[dict[str, Any]], now: str | None = None) -> dict[str, Any]:
    _ = now or iso_now()
    skills_dir = spec.home / "skills"
    profile: dict[str, Any] = {
        "profile": spec.name,
        "home": spec.display_home,
        "host_home": str(spec.home),
        "apply_allowed": spec.apply_allowed,
        "skills_dir_exists": skills_dir.exists(),
        "hub_installed": [],
        "bundled": [],
        "update_candidates": [],
        "findings": [],
        "errors": [],
        "warnings": [],
    }
    if not skills_dir.exists():
        profile["errors"].append({"code": "missing_skills_dir", "message": f"{skills_dir} does not exist"})
        profile["summary"] = report_summary([profile])
        return profile

    update_by_name = {str(row.get("name")): row for row in update_rows if row.get("name")}
    lock, lock_errors = load_hub_lock(skills_dir)
    profile["errors"].extend(lock_errors)
    installed = lock.get("installed", {}) if isinstance(lock, dict) else {}

    for name, entry in sorted(installed.items()):
        install_path = str(entry.get("install_path") or name)
        skill_dir = skills_dir / install_path
        lock_hash = str(entry.get("content_hash", ""))
        live_hash = hub_content_hash(skill_dir) if skill_dir.exists() else None
        update = update_by_name.get(name, {})
        update_status = str(update.get("status", "not_checked"))
        latest_hash = update.get("latest_hash")
        local_modified = bool(live_hash and lock_hash and live_hash != lock_hash)
        hub_row = {
            "name": name,
            "path": install_path,
            "source": entry.get("source"),
            "identifier": entry.get("identifier"),
            "trust_level": entry.get("trust_level"),
            "scan_verdict": entry.get("scan_verdict"),
            "lock_hash": lock_hash,
            "live_hash": live_hash,
            "latest_hash": latest_hash,
            "update_status": update_status,
            "local_modified": local_modified,
            "exists": skill_dir.exists(),
            "skill_md_exists": (skill_dir / "SKILL.md").exists(),
        }
        profile["hub_installed"].append(hub_row)
        if update_status == "update_available":
            profile["update_candidates"].append({k: hub_row[k] for k in ("name", "path", "source", "lock_hash", "live_hash", "latest_hash", "local_modified")})

        base_finding = {
            "profile": spec.name,
            "home": spec.display_home,
            "host_home": str(spec.home),
            "skill": name,
            "path": install_path,
            "provenance": f"hub:{entry.get('source', 'unknown')}",
            "lock_hash": lock_hash,
            "live_hash": live_hash,
            "latest_hash": latest_hash,
            "source": entry.get("source"),
            "trust_level": entry.get("trust_level"),
            "scan_verdict": entry.get("scan_verdict"),
            "update_status": update_status,
        }
        if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
            profile["findings"].append({
                **base_finding,
                "finding": "hub_skill_missing_or_invalid",
                "classification": "blocked",
                "recommended_decision": "restore_or_uninstall_broken_hub_skill",
                "risk": "high",
                "rationale": "Hub lock tracks this skill, but its install directory or SKILL.md is missing.",
            })
        elif local_modified and update_status == "update_available":
            profile["findings"].append({
                **base_finding,
                "finding": "hub_locally_modified_and_upstream_update_available",
                "classification": "manual_review",
                "recommended_decision": "rebase_local_edits_onto_upstream",
                "risk": "medium",
                "rationale": "Live on-disk hash differs from skills/.hub/lock.json and upstream has an update; raw `hermes skills update` would clobber local edits.",
            })
        elif local_modified:
            profile["findings"].append({
                **base_finding,
                "finding": "hub_locally_modified",
                "classification": "manual_review",
                "recommended_decision": "decide_keep_rebase_or_promote_local_edits",
                "risk": "medium",
                "rationale": "Live on-disk hash differs from skills/.hub/lock.json; preserve and review before any hub update.",
            })
        elif update_status == "update_available":
            classification, recommendation, risk, rationale = classify_hub_update(name, entry, spec)
            profile["findings"].append({
                **base_finding,
                "finding": "hub_update_available",
                "classification": classification,
                "recommended_decision": recommendation,
                "risk": risk,
                "rationale": rationale,
            })
        elif update_status == "unavailable":
            profile["findings"].append({
                **base_finding,
                "finding": "hub_update_source_unavailable",
                "classification": "blocked",
                "recommended_decision": "fix_or_remove_unavailable_hub_source",
                "risk": "medium",
                "rationale": "Hermes hub update discovery could not fetch the source for this installed skill.",
            })

    manifest, manifest_errors = load_bundled_manifest(skills_dir)
    profile["errors"].extend(manifest_errors)
    for name, origin_hash in sorted(manifest.items()):
        skill_dir = find_skill_dir(skills_dir, name)
        live_hash = bundled_dir_hash(skill_dir) if skill_dir else None
        row = {
            "name": name,
            "path": str(skill_dir.relative_to(skills_dir)) if skill_dir else None,
            "origin_hash": origin_hash,
            "live_hash": live_hash,
            "exists": skill_dir is not None,
            "user_modified": bool(skill_dir and origin_hash and live_hash != origin_hash),
        }
        profile["bundled"].append(row)
        if not skill_dir:
            profile["findings"].append({
                "profile": spec.name,
                "home": spec.display_home,
                "host_home": str(spec.home),
                "skill": name,
                "path": None,
                "provenance": "bundled",
                "finding": "bundled_manifest_entry_missing",
                "classification": "blocked",
                "recommended_decision": "repair_bundled_manifest_or_restore_skill",
                "risk": "medium",
                "origin_hash": origin_hash,
                "live_hash": None,
                "rationale": "Bundled manifest tracks this skill, but no matching SKILL.md directory was found.",
            })
        elif origin_hash and live_hash != origin_hash:
            profile["findings"].append({
                "profile": spec.name,
                "home": spec.display_home,
                "host_home": str(spec.home),
                "skill": name,
                "path": str(skill_dir.relative_to(skills_dir)),
                "provenance": "bundled",
                "finding": "bundled_skill_user_modified",
                "classification": "manual_review",
                "recommended_decision": "preserve_and_compare_with_current_bundled_source",
                "risk": "medium",
                "origin_hash": origin_hash,
                "live_hash": live_hash,
                "rationale": "Bundled manifest origin hash differs from live on-disk hash; preserve local edits and do not run reset --restore unattended.",
            })

    profile["summary"] = report_summary([profile])
    return profile


def docker_update_probe(profile: ProfileSpec, timeout: int = 180) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not profile.container_home:
        return [], {"code": "no_container_home", "message": "profile has no container_home mapping"}
    probe = """
import json, os, pathlib, shutil, tempfile

target = pathlib.Path(os.environ["TARGET_HERMES_HOME"])
with tempfile.TemporaryDirectory(prefix="skill-maintenance-check-") as td:
    temp_home = pathlib.Path(td)
    hub = temp_home / "skills" / ".hub"
    hub.mkdir(parents=True, exist_ok=True)
    source_hub = target / "skills" / ".hub"
    for filename in ("lock.json", "taps.json"):
        src = source_hub / filename
        if src.exists():
            shutil.copy2(src, hub / filename)
    os.environ["HERMES_HOME"] = str(temp_home)
    from tools.skills_hub import check_for_skill_updates
    rows = []
    for row in check_for_skill_updates():
        row = dict(row)
        row.pop("bundle", None)
        rows.append(row)
    print(json.dumps(rows, sort_keys=True))
"""
    cmd = [
        "docker", "exec",
        "-w", "/workspace/hermes-agent-private",
        "-e", f"PYTHONPATH={GATEWAY_PYTHONPATH}",
        "-e", f"TARGET_HERMES_HOME={profile.container_home}",
        GATEWAY_CONTAINER,
        GATEWAY_PYTHON,
        "-c", probe,
    ]
    proc = subprocess.run(cmd, cwd=str(STACK_DIR), text=True, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        return [], {
            "code": "hub_update_probe_failed",
            "message": f"docker update probe failed for {profile.display_home}",
            "returncode": proc.returncode,
            "stderr": proc.stderr[-4000:],
            "stdout": proc.stdout[-1000:],
        }
    try:
        rows = json.loads(proc.stdout)
        if not isinstance(rows, list):
            raise ValueError("expected list")
        return rows, None
    except Exception as exc:
        return [], {
            "code": "hub_update_probe_invalid_json",
            "message": f"update probe returned invalid JSON: {exc}",
            "stdout": proc.stdout[-4000:],
        }


def allowed_actions_for(finding: dict[str, Any]) -> list[str]:
    provenance = str(finding.get("provenance", ""))
    if provenance.startswith("hub:"):
        if finding.get("classification") == "auto_apply_ok":
            return ["plan_update", "apply_after_backup", "defer"]
        return ["keep_local", "rebase", "reset_to_upstream", "promote_local", "archive"]
    if provenance == "bundled":
        return ["keep_local", "compare_with_bundled", "reset_to_bundled", "promote_local", "archive"]
    return ["review", "ignore"]


def owner_for(finding: dict[str, Any]) -> str:
    if finding.get("finding") in {"hub_update_probe_failed", "missing_skills_dir"}:
        return "infrastructure"
    return "developer"


def make_decision_record(finding: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    now = now or iso_now()
    day = now[:10].replace("-", "")
    profile = safe_slug(str(finding.get("profile", "unknown")))
    skill = safe_slug(str(finding.get("skill", "unknown")))
    stable_payload = json.dumps({
        "profile": finding.get("profile"),
        "home": finding.get("home"),
        "skill": finding.get("skill"),
        "path": finding.get("path"),
        "finding": finding.get("finding"),
        "classification": finding.get("classification"),
        "lock_hash": finding.get("lock_hash") or finding.get("origin_hash"),
        "live_hash": finding.get("live_hash"),
        "latest_hash": finding.get("latest_hash"),
    }, sort_keys=True, default=str)
    short_hash = hashlib.sha256(stable_payload.encode("utf-8")).hexdigest()[:6]
    classification = finding.get("classification")
    delivery = ["vault", DISCORD_TARGET] if classification in {"manual_review", "blocked"} else ["vault"]
    return {
        "id": f"skilldec-{day}-{profile}-{skill}-{short_hash}",
        "created_at": now,
        "profile": finding.get("profile"),
        "home": finding.get("home"),
        "host_home": finding.get("host_home"),
        "skill": finding.get("skill"),
        "path": finding.get("path"),
        "provenance": finding.get("provenance"),
        "finding": finding.get("finding"),
        "classification": classification,
        "recommended_decision": finding.get("recommended_decision"),
        "allowed_actions": allowed_actions_for(finding),
        "risk": finding.get("risk"),
        "rationale": finding.get("rationale"),
        "artifacts": {},
        "status": "open",
        "owner": owner_for(finding),
        "delivery": delivery,
        "applied_by": None,
        "applied_at": None,
        "finding_snapshot": finding,
    }


def report_summary(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    findings = [f for p in profiles for f in p.get("findings", [])]
    errors = [e for p in profiles for e in p.get("errors", [])]
    warnings = [w for p in profiles for w in p.get("warnings", [])]
    return {
        "profiles": len(profiles),
        "hub_installed": sum(len(p.get("hub_installed", [])) for p in profiles),
        "bundled_tracked": sum(len(p.get("bundled", [])) for p in profiles),
        "update_candidates": sum(len(p.get("update_candidates", [])) for p in profiles),
        "findings": len(findings),
        "manual_review": sum(1 for f in findings if f.get("classification") == "manual_review"),
        "blocked": sum(1 for f in findings if f.get("classification") == "blocked"),
        "auto_apply_ok": sum(1 for f in findings if f.get("classification") == "auto_apply_ok"),
        "report_only": sum(1 for f in findings if f.get("classification") == "report_only"),
        "errors": len(errors),
        "warnings": len(warnings),
    }


def build_report(*, offline: bool = False, now: str | None = None) -> dict[str, Any]:
    now = now or iso_now()
    profiles: list[dict[str, Any]] = []
    for spec in discover_profiles():
        update_rows: list[dict[str, Any]] = []
        probe_error = None
        if not offline:
            update_rows, probe_error = docker_update_probe(spec)
        profile = inspect_profile(spec, update_rows=update_rows, now=now)
        if probe_error:
            profile["errors"].append(probe_error)
            profile.setdefault("findings", []).append({
                "profile": spec.name,
                "home": spec.display_home,
                "host_home": str(spec.home),
                "skill": "hub-update-probe",
                "path": None,
                "provenance": "hub",
                "finding": "hub_update_probe_failed",
                "classification": "blocked",
                "recommended_decision": "fix_skill_update_probe_runtime",
                "risk": "medium",
                "rationale": probe_error.get("message"),
            })
            profile["summary"] = report_summary([profile])
        profiles.append(profile)
    summary = report_summary(profiles)
    status = "error" if summary["errors"] else "ok"
    return {
        "stage": "skill-maintenance-report",
        "status": status,
        "generated_at": now,
        "stack_dir": str(STACK_DIR),
        "mode": "report",
        "read_only": True,
        "offline": offline,
        "summary": summary,
        "profiles": profiles,
    }


def collect_decisions(report: dict[str, Any]) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    now = str(report.get("generated_at") or iso_now())
    for profile in report.get("profiles", []):
        for finding in profile.get("findings", []):
            if finding.get("classification") in DECISION_CLASSIFICATIONS:
                decisions.append(make_decision_record(finding, now=now))
    return decisions


def merge_queue(existing_path: Path, decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    if existing_path.exists():
        try:
            for line in existing_path.read_text().splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("id"):
                    merged[item["id"]] = item
        except (OSError, json.JSONDecodeError):
            merged = {}
    for decision in decisions:
        previous = merged.get(decision["id"])
        if previous and previous.get("status") not in {None, "open"}:
            decision["status"] = previous["status"]
            decision["applied_by"] = previous.get("applied_by")
            decision["applied_at"] = previous.get("applied_at")
        merged[decision["id"]] = decision
    return sorted(merged.values(), key=lambda d: (str(d.get("status")), str(d.get("id"))))


def write_vault_notes(report: dict[str, Any], decisions: list[dict[str, Any]], queue: list[dict[str, Any]]) -> dict[str, str]:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    day = str(report.get("generated_at", iso_now()))[:10]
    daily = VAULT_DIR / f"{day}.md"
    open_decisions = VAULT_DIR / "open-decisions.md"
    summary = report.get("summary", {})
    lines = [
        f"# Skill maintenance report — {day}",
        "",
        f"Generated: `{report.get('generated_at')}`",
        f"Status: `{report.get('status')}`",
        "",
        "## Summary",
        "",
        f"- Profiles: {summary.get('profiles', 0)}",
        f"- Update candidates: {summary.get('update_candidates', 0)}",
        f"- Findings: {summary.get('findings', 0)}",
        f"- Manual review: {summary.get('manual_review', 0)}",
        f"- Blocked: {summary.get('blocked', 0)}",
        f"- Auto-apply candidates: {summary.get('auto_apply_ok', 0)}",
        "",
        "## Decisions from this run",
        "",
    ]
    if decisions:
        for decision in decisions:
            lines.extend([
                f"### {decision['id']}",
                "",
                f"- Profile: `{decision.get('profile')}`",
                f"- Skill: `{decision.get('skill')}`",
                f"- Classification: `{decision.get('classification')}`",
                f"- Finding: `{decision.get('finding')}`",
                f"- Recommended decision: `{decision.get('recommended_decision')}`",
                f"- Owner: `{decision.get('owner')}`",
                f"- Rationale: {decision.get('rationale')}",
                "",
            ])
    else:
        lines.append("No decisions generated.")
    daily.write_text("\n".join(lines).rstrip() + "\n")

    qlines = ["# Open skill-maintenance decisions", ""]
    open_items = [d for d in queue if d.get("status") == "open"]
    if open_items:
        for decision in open_items:
            qlines.append(
                f"- `{decision['id']}` — `{decision.get('profile')}/{decision.get('skill')}` "
                f"{decision.get('classification')} — {decision.get('recommended_decision')}"
            )
    else:
        qlines.append("No open decisions.")
    open_decisions.write_text("\n".join(qlines).rstrip() + "\n")
    return {"daily": str(daily), "open_decisions": str(open_decisions)}


def write_discord_summary(report: dict[str, Any], decisions: list[dict[str, Any]], vault_paths: dict[str, str]) -> str:
    actionable = [d for d in decisions if d.get("classification") in {"manual_review", "blocked"}]
    summary_path = STATE_DIR / "last-discord-summary.txt"
    if not actionable:
        text = "Skill maintenance: no actionable decisions.\n"
    else:
        lines = [f"Skill maintenance: {len(actionable)} decisions need review"]
        for decision in actionable[:12]:
            lines.append(
                f"- {decision.get('profile')}/{decision.get('skill')}: "
                f"{decision.get('finding')}; recommend {decision.get('recommended_decision')} ({decision.get('id')})"
            )
        if len(actionable) > 12:
            lines.append(f"- ... {len(actionable) - 12} more")
        lines.extend(["", f"Vault: {vault_paths.get('daily')}", f"Queue: {STATE_DIR / 'decision-queue.jsonl'}"])
        text = "\n".join(lines).rstrip() + "\n"
    summary_path.write_text(text)
    return str(summary_path)


def write_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    DECISION_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = str(report.get("generated_at", iso_now())).replace(":", "").replace("-", "")
    report_path = REPORT_DIR / f"{timestamp}.json"
    last_report = STATE_DIR / "last-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    last_report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    decisions = collect_decisions(report)
    for decision in decisions:
        decision_path = DECISION_DIR / f"{decision['id']}.json"
        decision_path.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n")
    queue_path = STATE_DIR / "decision-queue.jsonl"
    queue = merge_queue(queue_path, decisions)
    queue_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in queue))
    vault_paths = write_vault_notes(report, decisions, queue)
    discord_summary = write_discord_summary(report, decisions, vault_paths)
    return {
        "report": str(report_path),
        "last_report": str(last_report),
        "decision_queue": str(queue_path),
        "decisions": [str(DECISION_DIR / f"{d['id']}.json") for d in decisions],
        "vault": vault_paths,
        "discord_summary": discord_summary,
    }


def emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    summary = report.get("summary", {})
    print(f"status: {report.get('status')}")
    print(f"profiles: {summary.get('profiles', 0)}")
    print(f"update_candidates: {summary.get('update_candidates', 0)}")
    print(f"findings: {summary.get('findings', 0)}")
    print(f"manual_review: {summary.get('manual_review', 0)} blocked: {summary.get('blocked', 0)} auto_apply_ok: {summary.get('auto_apply_ok', 0)}")
    artifacts = report.get("artifacts") or {}
    if artifacts:
        print(f"last_report: {artifacts.get('last_report')}")
        print(f"decision_queue: {artifacts.get('decision_queue')}")
        print(f"discord_summary: {artifacts.get('discord_summary')}")


def timestamp_slug(value: str) -> str:
    return value.replace(":", "").replace("-", "")


def profile_from_name(profile: str) -> ProfileSpec | None:
    for spec in discover_profiles():
        if spec.name == profile:
            return spec
    return None


def profile_spec_from_finding(finding: dict[str, Any]) -> ProfileSpec:
    profile = safe_slug(str(finding.get("profile", "unknown")))
    host_home = Path(str(finding.get("host_home") or finding.get("home") or ""))
    container_home = str(finding.get("home")) if str(finding.get("home", "")).startswith("/opt/data") else None
    return ProfileSpec(
        name=profile,
        home=host_home,
        apply_allowed=profile in APPLY_PROFILE_NAMES,
        container_home=container_home,
    )


def snapshot_profile_state(spec: ProfileSpec) -> dict[str, Any]:
    skills_dir = spec.home / "skills"
    lock, lock_errors = load_hub_lock(skills_dir)
    manifest, manifest_errors = load_bundled_manifest(skills_dir)
    skill_paths = [str(p.relative_to(skills_dir)) for p in iter_skill_dirs(skills_dir)] if skills_dir.exists() else []
    return {
        "profile": spec.name,
        "home": spec.display_home,
        "host_home": str(spec.home),
        "skills_dir_exists": skills_dir.exists(),
        "hub_lock": lock,
        "bundled_manifest": manifest,
        "skill_paths": skill_paths,
        "errors": lock_errors + manifest_errors,
    }


def verify_backup_archive(archive: Path) -> dict[str, Any]:
    if not archive.exists():
        return {"archive": str(archive), "verified": False, "members": [], "error": "missing_archive"}
    proc = subprocess.run(
        ["tar", "--zstd", "-tf", str(archive)],
        text=True,
        capture_output=True,
        timeout=120,
    )
    members = [line for line in proc.stdout.splitlines() if line]
    verified = proc.returncode == 0 and any(m.startswith("skills/") or m == "skills" for m in members)
    result: dict[str, Any] = {
        "archive": str(archive),
        "verified": verified,
        "members": members[:200],
        "member_count": len(members),
    }
    if proc.returncode != 0:
        result["error"] = proc.stderr[-2000:]
    return result


def create_profile_backup(
    spec: ProfileSpec,
    *,
    backup_id: str,
    backup_root: Path = BACKUP_DIR,
    reason: str = "skill-maintenance",
) -> dict[str, Any]:
    skills_dir = spec.home / "skills"
    profile_slug = safe_slug(spec.name)
    profile_backup_dir = backup_root / backup_id / profile_slug
    profile_backup_dir.mkdir(parents=True, exist_ok=True)
    archive = profile_backup_dir / "skills.tar.zst"
    pre_state = profile_backup_dir / "pre-state.json"
    manifest_path = profile_backup_dir / "backup-manifest.json"
    state = snapshot_profile_state(spec)
    pre_state.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n")
    if not skills_dir.exists():
        result = {
            "profile": spec.name,
            "home": spec.display_home,
            "host_home": str(spec.home),
            "archive": str(archive),
            "pre_state": str(pre_state),
            "verified": False,
            "error": "missing_skills_dir",
            "reason": reason,
        }
        manifest_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result
    proc = subprocess.run(
        ["tar", "--zstd", "-cf", str(archive), "-C", str(spec.home), "skills"],
        text=True,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        result = {
            "profile": spec.name,
            "home": spec.display_home,
            "host_home": str(spec.home),
            "archive": str(archive),
            "pre_state": str(pre_state),
            "verified": False,
            "error": proc.stderr[-2000:],
            "reason": reason,
        }
        manifest_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result
    verify = verify_backup_archive(archive)
    result = {
        "profile": spec.name,
        "home": spec.display_home,
        "host_home": str(spec.home),
        "backup_id": backup_id,
        "archive": str(archive),
        "pre_state": str(pre_state),
        "manifest": str(manifest_path),
        "verified": bool(verify.get("verified")),
        "members": verify.get("members", []),
        "member_count": verify.get("member_count", 0),
        "reason": reason,
    }
    if not result["verified"]:
        result["error"] = verify.get("error", "archive_verification_failed")
    manifest_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def verify_profile_skills(home: Path) -> dict[str, Any]:
    skills_dir = home / "skills"
    errors: list[dict[str, Any]] = []
    if not skills_dir.exists():
        errors.append({"code": "missing_skills_dir", "message": f"{skills_dir} does not exist"})
    lock_path = skills_dir / ".hub" / "lock.json"
    if lock_path.exists():
        data = read_json(lock_path, None)
        if not isinstance(data, dict) or "__error__" in data:
            errors.append({"code": "invalid_hub_lock", "path": str(lock_path), "message": str(data)})
    for skill_dir in iter_skill_dirs(skills_dir):
        if not (skill_dir / "SKILL.md").exists():
            errors.append({"code": "missing_skill_md", "path": str(skill_dir)})
    return {"ok": not errors, "errors": errors, "skill_count": len(iter_skill_dirs(skills_dir))}


def auto_apply_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for profile in report.get("profiles", []):
        for finding in profile.get("findings", []):
            if finding.get("classification") != "auto_apply_ok":
                continue
            if finding.get("provenance") != "hub:official":
                continue
            if finding.get("skill") not in APPLY_ALLOWLIST:
                continue
            if finding.get("update_status") != "update_available":
                continue
            merged = dict(finding)
            merged.setdefault("host_home", profile.get("host_home"))
            merged.setdefault("home", profile.get("home"))
            merged.setdefault("profile", profile.get("profile"))
            findings.append(merged)
    return findings


def write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def build_plan(
    *,
    report: dict[str, Any] | None = None,
    now: str | None = None,
    state_dir: Path = STATE_DIR,
    backup_dir: Path = BACKUP_DIR,
    write: bool = True,
) -> dict[str, Any]:
    now = now or iso_now()
    plan_id = timestamp_slug(now)
    if report is None:
        report = build_report(now=now)
    updates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    backups_by_profile: dict[str, dict[str, Any]] = {}
    if report.get("status") != "ok":
        errors.append({"code": "report_not_ok", "message": "plan requires a clean report", "report_status": report.get("status")})
    for finding in auto_apply_findings(report):
        spec = profile_spec_from_finding(finding)
        if not spec.apply_allowed:
            continue
        backup = backups_by_profile.get(spec.name)
        if backup is None:
            backup = create_profile_backup(spec, backup_id=plan_id, backup_root=backup_dir, reason="plan")
            backups_by_profile[spec.name] = backup
        artifact_root = state_dir / "artifacts" / plan_id / safe_slug(spec.name) / safe_slug(str(finding.get("skill")))
        artifact_root.mkdir(parents=True, exist_ok=True)
        candidate_summary = artifact_root / "summary.md"
        candidate_summary.write_text(
            "\n".join([
                f"# Skill update candidate: {spec.name}/{finding.get('skill')}",
                "",
                f"- Path: `{finding.get('path')}`",
                f"- Source: `{finding.get('source')}`",
                f"- Current/live hash: `{finding.get('live_hash')}`",
                f"- Latest hash: `{finding.get('latest_hash')}`",
                f"- Backup: `{backup.get('archive')}`",
                "",
                "Diff/candidate expansion can be added here; this plan gates mutation on exact live hash and verified backup.",
            ]).rstrip() + "\n"
        )
        update = {
            "profile": spec.name,
            "home": spec.display_home,
            "host_home": str(spec.home),
            "skill": finding.get("skill"),
            "path": finding.get("path"),
            "identifier": finding.get("identifier"),
            "source": finding.get("source"),
            "expected_lock_hash": finding.get("lock_hash"),
            "expected_live_hash": finding.get("live_hash"),
            "expected_latest_hash": finding.get("latest_hash"),
            "status": "planned" if backup.get("verified") else "blocked",
            "backup": backup,
            "artifacts": {"summary": str(candidate_summary)},
            "rationale": finding.get("rationale"),
        }
        if not backup.get("verified"):
            update["blocker"] = "backup_verification_failed"
            errors.append({"code": "backup_verification_failed", "profile": spec.name, "skill": finding.get("skill"), "backup": backup})
        updates.append(update)
    status = "ok" if not errors else "blocked"
    plan = {
        "stage": "skill-maintenance-plan",
        "mode": "plan",
        "status": status,
        "plan_id": plan_id,
        "generated_at": now,
        "read_only": True,
        "report_generated_at": report.get("generated_at"),
        "summary": {
            "planned_updates": sum(1 for u in updates if u.get("status") == "planned"),
            "blocked_updates": sum(1 for u in updates if u.get("status") == "blocked"),
            "backups": len(backups_by_profile),
            "errors": len(errors),
        },
        "updates": updates,
        "errors": errors,
    }
    if write:
        plan_dir = state_dir / "plans"
        write_json_artifact(plan_dir / f"{plan_id}.json", plan)
        write_json_artifact(state_dir / "last-plan.json", plan)
    return plan


def load_plan(path: Path | None = None) -> dict[str, Any]:
    path = path or (STATE_DIR / "last-plan.json")
    data = read_json(path, None)
    if not isinstance(data, dict) or "__error__" in data:
        return {"status": "blocked", "errors": [{"code": "invalid_plan", "path": str(path), "message": str(data)}], "updates": []}
    return data


def run_skill_update(container_home: str, skill: str, timeout: int = 240) -> dict[str, Any]:
    cmd = [
        "docker", "exec",
        "-w", "/workspace/hermes-agent-private",
        "-e", f"PYTHONPATH={GATEWAY_PYTHONPATH}",
        "-e", f"HERMES_HOME={container_home}",
        GATEWAY_CONTAINER,
        "/opt/hermes/.venv/bin/hermes",
        "skills", "update", skill,
    ]
    proc = subprocess.run(cmd, cwd=str(STACK_DIR), text=True, capture_output=True, timeout=timeout)
    return {"returncode": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "cmd": cmd}


def lock_hash_for_skill(home: Path, skill: str) -> str | None:
    lock, _ = load_hub_lock(home / "skills")
    entry = lock.get("installed", {}).get(skill) if isinstance(lock, dict) else None
    if isinstance(entry, dict):
        value = entry.get("content_hash")
        return str(value) if value else None
    return None


def apply_plan(
    plan: dict[str, Any],
    *,
    confirm: str | None,
    dry_run: bool = False,
    state_dir: Path = STATE_DIR,
    backup_dir: Path = BACKUP_DIR,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or iso_now()
    apply_id = timestamp_slug(now)
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    if confirm != "UPDATE-HERMES-SKILLS":
        result = {
            "stage": "skill-maintenance-apply",
            "mode": "apply",
            "status": "blocked",
            "generated_at": now,
            "dry_run": dry_run,
            "errors": [{"code": "missing_or_invalid_confirmation", "message": "pass --confirm UPDATE-HERMES-SKILLS"}],
            "updates": [],
        }
        return result
    if plan.get("status") != "ok":
        errors.append({"code": "plan_not_ok", "message": "apply requires an ok plan", "plan_status": plan.get("status")})
    backups_by_profile: dict[str, dict[str, Any]] = {}
    for update in plan.get("updates", []):
        item = dict(update)
        home = Path(str(update.get("host_home") or ""))
        skill_path = str(update.get("path") or update.get("skill") or "")
        skill_dir = home / "skills" / skill_path
        live_hash = hub_content_hash(skill_dir) if skill_dir.exists() else None
        item["live_hash"] = live_hash
        if update.get("status") != "planned":
            item["status"] = "blocked"
            item["blocker"] = "not_planned"
            errors.append({"code": "update_not_planned", "profile": update.get("profile"), "skill": update.get("skill")})
            results.append(item)
            continue
        if live_hash != update.get("expected_live_hash"):
            item["status"] = "blocked"
            item["blocker"] = "live_hash_changed_since_plan"
            errors.append({
                "code": "live_hash_changed_since_plan",
                "profile": update.get("profile"),
                "skill": update.get("skill"),
                "expected": update.get("expected_live_hash"),
                "actual": live_hash,
            })
            results.append(item)
            continue
        plan_backup = update.get("backup", {})
        verify = verify_backup_archive(Path(str(plan_backup.get("archive", ""))))
        if not verify.get("verified"):
            item["status"] = "blocked"
            item["blocker"] = "plan_backup_not_verified"
            errors.append({"code": "plan_backup_not_verified", "profile": update.get("profile"), "skill": update.get("skill")})
            results.append(item)
            continue
        if dry_run:
            item["status"] = "dry_run_ok"
            results.append(item)
            continue
        profile = safe_slug(str(update.get("profile")))
        backup = backups_by_profile.get(profile)
        if backup is None:
            spec = ProfileSpec(name=profile, home=home, apply_allowed=True, container_home=str(update.get("home")))
            backup = create_profile_backup(spec, backup_id=apply_id, backup_root=backup_dir, reason="apply")
            backups_by_profile[profile] = backup
        item["apply_backup"] = backup
        if not backup.get("verified"):
            item["status"] = "blocked"
            item["blocker"] = "apply_backup_not_verified"
            errors.append({"code": "apply_backup_not_verified", "profile": profile, "skill": update.get("skill")})
            results.append(item)
            continue
        run = run_skill_update(str(update.get("home")), str(update.get("skill")))
        item["update_command"] = {k: v for k, v in run.items() if k != "cmd"}
        if run.get("returncode") != 0:
            rollback = restore_profile_backup(Path(str(backup.get("archive"))), home)
            item["status"] = "rolled_back"
            item["rollback"] = rollback
            errors.append({"code": "skill_update_failed", "profile": profile, "skill": update.get("skill"), "stderr": run.get("stderr")})
            results.append(item)
            continue
        post_lock_hash = lock_hash_for_skill(home, str(update.get("skill")))
        item["post_lock_hash"] = post_lock_hash
        item["post_live_hash"] = hub_content_hash(skill_dir) if skill_dir.exists() else None
        verify_profile = verify_profile_skills(home)
        item["post_verify"] = verify_profile
        if post_lock_hash != update.get("expected_latest_hash") or not verify_profile.get("ok"):
            rollback = restore_profile_backup(Path(str(backup.get("archive"))), home)
            item["status"] = "rolled_back"
            item["rollback"] = rollback
            errors.append({
                "code": "post_update_verification_failed",
                "profile": profile,
                "skill": update.get("skill"),
                "expected_latest_hash": update.get("expected_latest_hash"),
                "post_lock_hash": post_lock_hash,
            })
        else:
            item["status"] = "applied"
        results.append(item)
    if errors:
        status = "blocked" if all(r.get("status") == "blocked" for r in results) else "error"
    else:
        status = "ok"
    result = {
        "stage": "skill-maintenance-apply",
        "mode": "apply",
        "status": status,
        "apply_id": apply_id,
        "generated_at": now,
        "dry_run": dry_run,
        "plan_id": plan.get("plan_id"),
        "summary": {
            "updates": len(results),
            "applied": sum(1 for r in results if r.get("status") == "applied"),
            "dry_run_ok": sum(1 for r in results if r.get("status") == "dry_run_ok"),
            "blocked": sum(1 for r in results if r.get("status") == "blocked"),
            "rolled_back": sum(1 for r in results if r.get("status") == "rolled_back"),
            "errors": len(errors),
        },
        "updates": results,
        "errors": errors,
    }
    apply_dir = state_dir / "applies"
    write_json_artifact(apply_dir / f"{apply_id}.json", result)
    write_json_artifact(state_dir / "last-apply.json", result)
    return result


def restore_profile_backup(archive: Path, profile_home: Path) -> dict[str, Any]:
    verify = verify_backup_archive(archive)
    if not verify.get("verified"):
        return {"status": "blocked", "archive": str(archive), "error": "backup_not_verified", "verify": verify}
    current = profile_home / "skills"
    staging = profile_home / f"skills.rollback-staging-{timestamp_slug(iso_now())}"
    try:
        if staging.exists():
            shutil.rmtree(staging)
        if current.exists():
            current.rename(staging)
        proc = subprocess.run(
            ["tar", "--zstd", "-xf", str(archive), "-C", str(profile_home)],
            text=True,
            capture_output=True,
            timeout=300,
        )
        if proc.returncode != 0:
            if current.exists():
                shutil.rmtree(current)
            if staging.exists():
                staging.rename(current)
            return {"status": "error", "archive": str(archive), "stderr": proc.stderr[-2000:]}
        post = verify_profile_skills(profile_home)
        if not post.get("ok"):
            if current.exists():
                shutil.rmtree(current)
            if staging.exists():
                staging.rename(current)
            return {"status": "error", "archive": str(archive), "post_verify": post}
        if staging.exists():
            shutil.rmtree(staging)
        return {"status": "ok", "archive": str(archive), "post_verify": post}
    except Exception as exc:  # noqa: BLE001 - rollback must return structured failure
        if not current.exists() and staging.exists():
            staging.rename(current)
        return {"status": "error", "archive": str(archive), "error": str(exc)}


def rollback_backup(
    *,
    backup_id: str,
    profile: str,
    confirm: str | None,
    backup_root: Path = BACKUP_DIR,
    profile_home: Path | None = None,
    state_dir: Path = STATE_DIR,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or iso_now()
    if confirm != "RESTORE-HERMES-SKILLS":
        return {
            "stage": "skill-maintenance-rollback",
            "mode": "rollback",
            "status": "blocked",
            "generated_at": now,
            "errors": [{"code": "missing_or_invalid_confirmation", "message": "pass --confirm RESTORE-HERMES-SKILLS"}],
        }
    profile_slug = safe_slug(profile)
    if profile_home is None:
        spec = profile_from_name(profile_slug)
        if spec is None:
            return {
                "stage": "skill-maintenance-rollback",
                "mode": "rollback",
                "status": "blocked",
                "generated_at": now,
                "errors": [{"code": "unknown_profile", "profile": profile}],
            }
        profile_home = spec.home
    archive = backup_root / backup_id / profile_slug / "skills.tar.zst"
    backup_manifest = backup_root / backup_id / profile_slug / "backup-manifest.json"
    backup = read_json(backup_manifest, {}) if backup_manifest.exists() else {"archive": str(archive)}
    restore = restore_profile_backup(archive, profile_home)
    result = {
        "stage": "skill-maintenance-rollback",
        "mode": "rollback",
        "status": restore.get("status"),
        "generated_at": now,
        "backup_id": backup_id,
        "profile": profile_slug,
        "host_home": str(profile_home),
        "backup": backup,
        "restore": restore,
        "errors": [] if restore.get("status") == "ok" else [{"code": "restore_failed", "detail": restore}],
    }
    rollback_id = timestamp_slug(now)
    rollback_dir = state_dir / "rollbacks"
    write_json_artifact(rollback_dir / f"{rollback_id}.json", result)
    write_json_artifact(state_dir / "last-rollback.json", result)
    return result


def cmd_plan(args: argparse.Namespace) -> int:
    report = None
    if args.report:
        report = read_json(Path(args.report), None)
        if not isinstance(report, dict) or "__error__" in report:
            result = {"stage": "skill-maintenance-plan", "status": "blocked", "errors": [{"code": "invalid_report", "path": args.report}]}
            emit(result, args.json)
            return 1
    plan = build_plan(report=report, write=not args.no_write)
    emit(plan, args.json)
    return 0 if plan.get("status") == "ok" else 1


def cmd_apply(args: argparse.Namespace) -> int:
    plan = load_plan(Path(args.plan) if args.plan else None)
    result = apply_plan(plan, confirm=args.confirm, dry_run=args.dry_run)
    emit(result, args.json)
    return 0 if result.get("status") == "ok" else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    result = rollback_backup(backup_id=args.backup_id, profile=args.profile, confirm=args.confirm)
    emit(result, args.json)
    return 0 if result.get("status") == "ok" else 1


def cmd_report(args: argparse.Namespace) -> int:
    report = build_report(offline=args.offline)
    if not args.no_write:
        report["artifacts"] = write_artifacts(report)
    emit(report, args.json)
    return 0 if report.get("status") == "ok" else 1


def unavailable_mode(name: str) -> int:
    report = {
        "stage": f"skill-maintenance-{name}",
        "status": "blocked",
        "generated_at": iso_now(),
        "message": f"{name} mode is intentionally not implemented in the report-only milestone.",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes skill maintenance lane")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="read-only skill inventory/update/decision report")
    report.add_argument("--json", action="store_true", help="emit JSON")
    report.add_argument("--offline", action="store_true", help="skip remote hub update discovery")
    report.add_argument("--no-write", action="store_true", help="do not write state/vault artifacts")

    plan = sub.add_parser("plan", help="create a guarded apply plan and verified backups")
    plan.add_argument("--json", action="store_true", help="emit JSON")
    plan.add_argument("--report", help="use an existing report JSON instead of running a fresh report")
    plan.add_argument("--no-write", action="store_true", help="do not write plan artifacts")

    apply = sub.add_parser("apply", help="apply an existing plan after confirmation")
    apply.add_argument("--json", action="store_true", help="emit JSON")
    apply.add_argument("--plan", help="plan JSON path; defaults to state/skill-maintenance/last-plan.json")
    apply.add_argument("--confirm", help="required: UPDATE-HERMES-SKILLS")
    apply.add_argument("--dry-run", action="store_true", help="verify plan/hash/backup gates without mutating skills")

    rollback = sub.add_parser("rollback", help="restore a profile from a skill-maintenance backup")
    rollback.add_argument("--json", action="store_true", help="emit JSON")
    rollback.add_argument("--backup-id", required=True, help="backup timestamp/id under backups/skill-maintenance")
    rollback.add_argument("--profile", required=True, help="profile slug to restore")
    rollback.add_argument("--confirm", help="required: RESTORE-HERMES-SKILLS")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "apply":
        return cmd_apply(args)
    if args.command == "rollback":
        return cmd_rollback(args)
    return unavailable_mode(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
