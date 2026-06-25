#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BOOTSTRAP = BASE_DIR / "scripts" / "bootstrap_profile_gateways.py"
GENERATE = BASE_DIR / "scripts" / "generate_profile_gateway_compose.py"


def run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, cwd=BASE_DIR, text=True, capture_output=True)
    return {
        "command": cmd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap, generate, and optionally deploy profile gateways")
    parser.add_argument("--apply", action="store_true", help="Run docker compose up -d --remove-orphans after generating")
    args = parser.parse_args()

    results = {
        "bootstrap": run(["python3", str(BOOTSTRAP)]),
        "generate": run(["python3", str(GENERATE)]),
    }

    if results["bootstrap"]["exit_code"] != 0 or results["generate"]["exit_code"] != 0:
        print(json.dumps(results, indent=2))
        raise SystemExit(1)

    if args.apply:
        results["apply"] = run(["docker", "compose", "up", "-d", "--remove-orphans"])
        if results["apply"]["exit_code"] != 0:
            print(json.dumps(results, indent=2))
            raise SystemExit(1)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
