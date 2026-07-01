#!/usr/bin/env python3
"""Compose-level drift guard for hermes-webui-stack.

Detects silent divergence between the current state of compose/script/profile
files and the last recorded fingerprint. Fails exit(1) if drift is found.

Canonical location (after deployment):
    /home/jmelchiori/docker-compose/hermes-webui-stack/scripts/check_compose_fingerprint.py

Auto-detects the stack root from its own location so it works both at the
canonical path and inside the workspace-relative path:
    stack_root/scripts/check_compose_fingerprint.py        → parents[1] = stack_root
    stack_root/workspace/scripts/check_compose_fingerprint.py → parents[2] = stack_root

Usage:
    python3 scripts/check_compose_fingerprint.py           # check only (exit 0=clean,1=drift)
    python3 scripts/check_compose_fingerprint.py --fix     # regenerate fingerprint
    python3 scripts/check_compose_fingerprint.py --stack-dir /custom/path  # explicit path
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Files to fingerprint ──────────────────────────────────────────────
WATCHED_FILES = [
    "docker-compose.yml",
    "docker-compose.override.yml",
    "docker-compose.caddy.yml",
    "config/Caddyfile",
    "scripts/generate_profile_gateway_compose.py",
    "Dockerfile.gateway",
]

FINGERPRINT_REL = "state/compose-fingerprint.json"
PROFILES_REL = "hermes-home/profiles"
SOUL_MARKER = "SOUL.md"


# ── Helpers ───────────────────────────────────────────────────────────

def find_stack_root(script_path: Path) -> Path | None:
    """Walk up from the script directory looking for docker-compose.yml."""
    candidates = [
        script_path.parent,           # scripts/  (canonical: stack_root/scripts/)
        script_path.parent.parent,    # workspace/scripts/ -> workspace/
        script_path.parent.parent.parent,  # workspace/scripts/ -> workspace/ -> stack_root
    ]
    for cand in candidates:
        if (cand / "docker-compose.yml").exists():
            return cand.resolve()
    return None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_profile_names(profiles_root: Path) -> list[str]:
    """Return sorted list of profile directory names that have a SOUL marker."""
    if not profiles_root.exists():
        return []
    return sorted(
        d.name for d in profiles_root.iterdir()
        if d.is_dir() and (d / SOUL_MARKER).exists()
    )


def compute_fingerprint(stack_dir: Path) -> dict:
    """Build the current fingerprint dict, preserving any existing extra keys."""
    fingerprint_path = stack_dir / FINGERPRINT_REL
    old = {}
    if fingerprint_path.exists():
        try:
            old = json.loads(fingerprint_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    fp = dict(old)  # preserve any extra fields from previous fingerprints

    for rel in WATCHED_FILES:
        path = stack_dir / rel
        if path.exists():
            fp[f"{rel}_sha256"] = sha256_of(path)

    profiles = get_profile_names(stack_dir / PROFILES_REL)
    fp["profiles"] = profiles
    fp["profile_count"] = len(profiles)
    fp["fingerprint_ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return fp


def check_fingerprint(stack_dir: Path) -> list[str]:
    """Return list of drift findings. Empty list = clean."""
    fingerprint_path = stack_dir / FINGERPRINT_REL
    if not fingerprint_path.exists():
        return [f"MISSING: {FINGERPRINT_REL} — run with --fix"]

    try:
        fp = json.loads(fingerprint_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return [f"CORRUPT: {FINGERPRINT_REL} — {e}"]

    issues = []

    # Check watched file hashes
    for rel in WATCHED_FILES:
        path = stack_dir / rel
        expected = fp.get(f"{rel}_sha256")
        if not path.exists():
            issues.append(f"MISSING: {rel}")
            continue
        if expected is None:
            issues.append(f"UNTRACKED: {rel} (not in fingerprint)")
            continue
        actual = sha256_of(path)
        if actual != expected:
            issues.append(f"DRIFT: {rel} hash changed")

    # Check profile list
    current_profiles = get_profile_names(stack_dir / PROFILES_REL)
    expected_profiles = fp.get("profiles", [])
    added = set(current_profiles) - set(expected_profiles)
    removed = set(expected_profiles) - set(current_profiles)
    if added:
        issues.append(f"NEW_PROFILES: {', '.join(sorted(added))}")
    if removed:
        issues.append(f"REMOVED_PROFILES: {', '.join(sorted(removed))}")

    return issues


# ── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    # Resolve stack directory
    if "--stack-dir" in sys.argv:
        idx = sys.argv.index("--stack-dir") + 1
        if idx >= len(sys.argv):
            print("ERROR: --stack-dir requires a path argument")
            return 2
        stack_dir = Path(sys.argv[idx]).resolve()
    else:
        detected = find_stack_root(Path(__file__).resolve())
        if detected is None:
            print("ERROR: cannot auto-detect stack root — specify --stack-dir")
            return 2
        stack_dir = detected

    if not (stack_dir / "docker-compose.yml").exists():
        print(f"ERROR: {stack_dir} does not contain docker-compose.yml")
        return 2

    fix_mode = "--fix" in sys.argv

    if fix_mode:
        # Regenerate fingerprint
        fingerprint_dir = stack_dir / FINGERPRINT_REL
        fingerprint_dir.parent.mkdir(parents=True, exist_ok=True)
        fp = compute_fingerprint(stack_dir)
        fingerprint_dir.write_text(json.dumps(fp, indent=2) + "\n")
        print(f"Fingerprint written: {fingerprint_dir}")
        print(f"  Stack dir: {stack_dir}")
        print(f"  Profiles:  {', '.join(fp['profiles'])}")
        print(f"  Timestamp: {fp['fingerprint_ts']}")
        return 0

    issues = check_fingerprint(stack_dir)
    if issues:
        print("DRIFT FOUND:")
        for i in issues:
            print(f"  - {i}")
        return 1

    print("OK — no drift detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
