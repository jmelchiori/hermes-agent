#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
HERMES_HOME = BASE_DIR / "hermes-home"
PROFILES_DIR = HERMES_HOME / "profiles"
ROOT_ENV = HERMES_HOME / ".env"
START_PORT = 8643

KEEP_TELEGRAM_FLAG = "PROFILE_GATEWAY_KEEP_TELEGRAM"
KEEP_DISCORD_FLAG = "PROFILE_GATEWAY_KEEP_DISCORD"


def parse_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def comment_out_keys(lines: list[str], prefixes: tuple[str, ...], reason: str) -> tuple[list[str], list[str]]:
    out: list[str] = []
    changed: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if any(key.startswith(prefix) for prefix in prefixes):
            out.append(f"# {key} disabled by bootstrap_profile_gateways.py: {reason}")
            changed.append(key)
        else:
            out.append(line)
    return out, changed


def upsert_setting(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    target = f"{key}={value}"
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        current_key = stripped.split("=", 1)[0].strip()
        if current_key == key:
            if line == target:
                return lines, False
            lines[idx] = target
            return lines, True
    if lines and lines[-1] != "":
        lines.append("")
    lines.append(target)
    return lines, True


def ensure_platform_block(config_path: Path, *, include_homeassistant: bool) -> bool:
    text = config_path.read_text() if config_path.exists() else ""
    if "platforms:" in text:
        return False
    block_lines = [
        "platforms:",
        "  api_server:",
        "    enabled: true",
        "    extra:",
        "      host: 0.0.0.0",
        "  telegram:",
        "    enabled: false",
        "  discord:",
        "    enabled: false",
    ]
    if include_homeassistant:
        block_lines.extend([
            "  homeassistant:",
            "    enabled: true",
            "    extra:",
            "      watch_entities:",
            "      - sun.sun",
        ])
    block = "\n".join(block_lines)
    new_text = text.rstrip() + "\n\n" + block + "\n"
    config_path.write_text(new_text)
    return True


def main() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    root_env = parse_env(ROOT_ENV)
    profiles = sorted([p for p in PROFILES_DIR.iterdir() if p.is_dir()])
    results: list[dict[str, object]] = []

    for offset, profile_dir in enumerate(profiles):
        name = profile_dir.name
        env_path = profile_dir / ".env"
        cfg_path = profile_dir / "config.yaml"
        env_lines = env_path.read_text().splitlines() if env_path.exists() else []
        env_map = parse_env(env_path)
        changed_keys: list[str] = []

        keep_telegram = env_map.get(KEEP_TELEGRAM_FLAG, "").lower() in {"1", "true", "yes"}
        keep_discord = env_map.get(KEEP_DISCORD_FLAG, "").lower() in {"1", "true", "yes"}

        if not keep_telegram:
            env_lines, changed = comment_out_keys(
                env_lines,
                ("TELEGRAM_",),
                "profile gateways do not inherit Telegram by default",
            )
            changed_keys.extend(changed)

        if not keep_discord:
            env_lines, changed = comment_out_keys(
                env_lines,
                ("DISCORD_",),
                "profile gateways do not inherit Discord by default",
            )
            changed_keys.extend(changed)

        port = str(START_PORT + offset)
        for key, value in [
            ("API_SERVER_ENABLED", "true"),
            ("API_SERVER_HOST", "0.0.0.0"),
            ("API_SERVER_PORT", port),
            ("OPENVIKING_ENDPOINT", "http://127.0.0.1:1933"),
            ("TZ", "America/Chicago"),
        ]:
            env_lines, changed = upsert_setting(env_lines, key, value)
            if changed:
                changed_keys.append(key)

        env_path.write_text("\n".join(env_lines).rstrip() + "\n")
        config_changed = ensure_platform_block(cfg_path, include_homeassistant=("HASS_TOKEN" in parse_env(env_path)))

        warnings: list[str] = []
        if not (profile_dir / "auth.json").exists() and (HERMES_HOME / "auth.json").exists():
            warnings.append("auth.json missing in profile; copy manually if this profile needs OAuth-backed providers")
        if not (profile_dir / "google_token.json").exists():
            warnings.append("google_token.json not present (expected; Google OAuth is not copied by default)")

        results.append({
            "profile": name,
            "api_port": int(port),
            "env_path": str(env_path),
            "config_path": str(cfg_path),
            "changed_keys": sorted(set(changed_keys)),
            "config_platform_block_added": config_changed,
            "warnings": warnings,
        })

    print(json.dumps({"profiles": results}, indent=2))


if __name__ == "__main__":
    main()
