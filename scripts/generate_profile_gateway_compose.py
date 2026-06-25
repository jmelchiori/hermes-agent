#!/usr/bin/env python3
"""Generate docker-compose.override.yml with per-profile gateway services."""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
HERMES_HOME = BASE_DIR / "hermes-home"
PROFILES_DIR = HERMES_HOME / "profiles"
OUTPUT_PATH = BASE_DIR / "docker-compose.override.yml"
START_PORT = 8643


def dump_service(name: str, port: int) -> str:
    service_name = f"hermes-gateway-{name}"
    container_name = f"hermes-webui-stack-gateway-{name}"
    profile_home = f"/home/hermeswebui/.hermes/profiles/{name}"
    profile_shell_home = f"{profile_home}/home"
    env_file_path = f"./hermes-home/profiles/{name}/.env"
    return f"""  {service_name}:
    build:
      context: .
      dockerfile: Dockerfile.gateway
    image: local/hermes-webui-stack-gateway:latest
    container_name: {container_name}
    user: "${{WANTED_UID}}:${{WANTED_GID}}"
    network_mode: service:tailscale
    depends_on:
      tailscale:
        condition: service_healthy
      hindsight:
        condition: service_healthy
      agenthub-api:
        condition: service_healthy
    env_file:
      - {env_file_path}
      - ./config/agenthub-client.env
    environment:
      HERMES_HOME: {profile_home}
      HERMES_CONFIG_PATH: {profile_home}/config.yaml
      HOME: {profile_shell_home}
      XDG_STATE_HOME: {profile_shell_home}/.local/state
      GH_CONFIG_DIR: {profile_home}/.config/gh
      PYTHONPATH: /workspace/hermes-agent-private
      PATH: {profile_shell_home}/.local/bin:/opt/hermes/.venv/bin:/home/hermeswebui/.hermes/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
      API_SERVER_ENABLED: "true"
      API_SERVER_HOST: 0.0.0.0
      API_SERVER_PORT: "{port}"
      HINDSIGHT_API_URL: http://127.0.0.1:8888
      TZ: ${{TZ:-America/Chicago}}
    secrets:
      - agenthub_api_token
    working_dir: /workspace
    command: ["gateway", "run"]
    volumes:
      - ./hermes-home:/home/hermeswebui/.hermes
      # Temporary compatibility alias while old scripts/config references are retired.
      - ./hermes-home:/opt/data
      - ./workspace:/workspace
    restart: unless-stopped"""


def main() -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profiles = sorted([p.name for p in PROFILES_DIR.iterdir() if p.is_dir()])
    lines = [
        "# GENERATED FILE -- DO NOT EDIT MANUALLY",
        "# Regenerate with: python3 scripts/generate_profile_gateway_compose.py",
        "services:",
    ]
    port_map: dict[str, int] = {}
    for idx, name in enumerate(profiles):
        port = START_PORT + idx
        port_map[name] = port
        lines.append(dump_service(name, port).rstrip())
    OUTPUT_PATH.write_text("\n".join(lines).rstrip() + "\n")
    print(json.dumps({"output": str(OUTPUT_PATH), "profiles": profiles, "api_ports": port_map}, indent=2))


if __name__ == "__main__":
    main()
