#!/usr/bin/env python3
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

STACK_DIR = Path.home() / "docker-compose" / "hermes-webui-stack"
SYNC_GH_SCRIPT = Path.home() / ".local" / "bin" / "sync-github-auth-from-host.py"
DOCKERFILE_GATEWAY = STACK_DIR / "Dockerfile.gateway"
WORKSPACE_GATEWAY_REPO = STACK_DIR / "workspace" / "hermes-agent-private"
PATCHES_DIR = STACK_DIR / "patches"
VERIFY_TIMEOUT = 180
GATEWAY_SERVICE_PREFIX = "hermes-gateway"
GATEWAY_IMAGE = "local/hermes-webui-stack-gateway:latest"
GATEWAY_PULL_REF = "nousresearch/hermes-agent:main"
WORKSPACE_MCP_TOOL = WORKSPACE_GATEWAY_REPO / "tools" / "mcp_tool.py"
WEBUI_RUNTIME_MCP_TOOL = "/app/venv/lib/python3.12/site-packages/tools/mcp_tool.py"
BUILD_WEBUI_REPO = STACK_DIR / "build" / "hermes-agent-private"
DOCKERFILE_WEBUI = STACK_DIR / "Dockerfile.webui"
GATEWAY_RUNTIME_MCP_TOOL = "/workspace/hermes-agent-private/tools/mcp_tool.py"

# Files in the fork that carry real bug fixes / local changes (non-p2p).
# These are diffed against upstream/main and the result is stored as patches.
# Excluded: agent/protocols/** (dead p2p protocol, superseded by AgentHub).
PATCH_SOURCE_FILES = [
    "agent/auxiliary_client.py",
    "hermes_state.py",
    "hermes_cli/commands.py",
    "hermes_cli/config.py",
    "hermes_cli/cron.py",
    "hermes_cli/main.py",
    "cron/jobs.py",
    "gateway/run.py",
    "run_agent.py",
    "tools/cronjob_tools.py",
    "tools/mcp_tool.py",
    "docker/entrypoint.sh",
    "agent/skill_commands.py",
    "cron/scheduler.py",
    "agent/usage_pricing.py",
]

CORE_SERVICES = {
    "tailscale": {
        "container": "hermes-webui-stack-tailscale",
        "image": "ghcr.io/tailscale/tailscale:stable",
        "pull_ref": "ghcr.io/tailscale/tailscale:stable",
        "healthcheck": "healthy",
        "kind": "tailscale",
    },
    "hermes-webui": {
        "container": "hermes-webui-stack-webui",
        "image": "local/hermes-webui-stack-webui:latest",
        "pull_ref": "ghcr.io/nesquena/hermes-webui:latest",
        "build": True,
        "healthcheck": "healthy",
        "kind": "webui",
    },
}


def run(cmd: list[str], check: bool = True, capture: bool = True, cwd: Optional[Path] = STACK_DIR) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        capture_output=capture,
    )


def get_image_id(ref: str) -> Optional[str]:
    proc = run(["docker", "image", "inspect", ref, "--format", "{{.Id}}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def get_image_meta(ref_or_id: Optional[str]) -> dict[str, Any]:
    if not ref_or_id:
        return {}
    proc = run(["docker", "image", "inspect", ref_or_id], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    data = json.loads(proc.stdout)[0]
    labels = ((data.get("Config") or {}).get("Labels") or {}) or {}
    repo_digests = data.get("RepoDigests") or []
    return {
        "id": data.get("Id"),
        "repo_digests": repo_digests,
        "version": labels.get("org.opencontainers.image.version") or labels.get("version") or "",
        "revision": labels.get("org.opencontainers.image.revision") or "",
        "created": data.get("Created") or labels.get("org.opencontainers.image.created") or "",
        "labels": labels,
    }


def compose_service_names() -> list[str]:
    proc = run(["docker", "compose", "config", "--services"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose config --services failed: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def compose_ps_rows() -> list[dict[str, Any]]:
    proc = run(["docker", "compose", "ps", "--all", "--format", "json"], check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                return []
        return rows
    return []


def expected_container_name(service: str) -> str:
    if service in CORE_SERVICES:
        return CORE_SERVICES[service]["container"]
    if service == GATEWAY_SERVICE_PREFIX:
        return "hermes-webui-stack-gateway"
    suffix = service.removeprefix(f"{GATEWAY_SERVICE_PREFIX}-")
    return f"hermes-webui-stack-gateway-{suffix}"


def discover_service_specs() -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for service in compose_service_names():
        if service in CORE_SERVICES:
            specs[service] = dict(CORE_SERVICES[service])
            continue
        if service == GATEWAY_SERVICE_PREFIX or service.startswith(f"{GATEWAY_SERVICE_PREFIX}-"):
            specs[service] = {
                "container": expected_container_name(service),
                "image": GATEWAY_IMAGE,
                "pull_ref": GATEWAY_PULL_REF,
                "build": service == GATEWAY_SERVICE_PREFIX,
                "healthcheck": "running",
                "kind": "gateway",
            }
    required = ["tailscale", "hermes-webui", GATEWAY_SERVICE_PREFIX]
    missing = [service for service in required if service not in specs]
    if missing:
        raise RuntimeError(f"missing required compose services: {', '.join(missing)}")
    return specs


def gateway_services(specs: dict[str, dict[str, Any]]) -> list[str]:
    return [service for service, spec in specs.items() if spec.get("kind") == "gateway"]


def runtime_services(specs: dict[str, dict[str, Any]]) -> list[str]:
    return ["hermes-webui", *gateway_services(specs)]


def get_container_image_id(container: str) -> Optional[str]:
    proc = run(["docker", "inspect", container, "--format", "{{.Image}}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def pull_refs(refs: list[str]) -> tuple[bool, str, str]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    for ref in refs:
        proc = run(["docker", "pull", ref], check=False)
        stdout_chunks.append(f"== {ref} ==\n{proc.stdout}")
        stderr_chunks.append(f"== {ref} ==\n{proc.stderr}")
        if proc.returncode != 0:
            return False, "\n".join(stdout_chunks), "\n".join(stderr_chunks)
    return True, "\n".join(stdout_chunks), "\n".join(stderr_chunks)


def sync_host_github_auth() -> dict[str, Any]:
    result: dict[str, Any] = {"component": "host-github-auth-sync", "script": str(SYNC_GH_SCRIPT)}
    if not SYNC_GH_SCRIPT.exists():
        result["error"] = "sync_script_missing"
        return result
    proc = run([str(SYNC_GH_SCRIPT)], check=False)
    result["stdout"] = proc.stdout.strip()
    result["stderr"] = proc.stderr.strip()
    if proc.returncode != 0:
        result["error"] = "sync_script_failed"
        return result
    try:
        payload = json.loads(proc.stdout)
        result.update(payload)
    except Exception:
        result["synced"] = True
    return result


def repo_status(repo: Path) -> list[str]:
    proc = run(["git", "status", "--short"], check=False, cwd=repo)
    if proc.returncode != 0:
        return [f"git status failed: {proc.stderr.strip()}"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


def repo_head(repo: Path) -> str:
    proc = run(["git", "rev-parse", "HEAD"], check=False, cwd=repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_branch(repo: Path) -> str:
    proc = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False, cwd=repo)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def rebase_workspace_from_upstream(dry_run: bool) -> dict[str, Any]:
    """Rebase workspace/hermes-agent-private onto upstream/main before patch extraction."""
    result: dict[str, Any] = {
        "component": "workspace-upstream-rebase",
        "workspace_repo": str(WORKSPACE_GATEWAY_REPO),
        "rebased": False,
        "pushed": False,
    }
    if not (WORKSPACE_GATEWAY_REPO / ".git").exists():
        result["error"] = "workspace_not_a_git_repo"
        return result

    remotes = run(["git", "remote"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
    if "upstream" not in remotes.stdout:
        result["error"] = "upstream_remote_missing"
        result["remotes"] = remotes.stdout.strip().splitlines()
        return result

    before_rev = repo_head(WORKSPACE_GATEWAY_REPO)
    result["before_rev"] = before_rev
    result["branch"] = repo_branch(WORKSPACE_GATEWAY_REPO)

    fetch = run(["git", "fetch", "upstream", "--quiet"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
    result["fetch_stdout"] = fetch.stdout[-2000:]
    result["fetch_stderr"] = fetch.stderr[-2000:]
    if fetch.returncode != 0:
        result["error"] = "upstream_fetch_failed"
        return result

    behind = run(["git", "rev-list", "--count", "HEAD..upstream/main"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
    behind_count = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else 0
    result["behind"] = behind_count
    if behind_count == 0:
        result["rebased"] = True
        result["after_rev"] = before_rev
        return result

    if dry_run:
        result["note"] = f"would rebase {behind_count} commits"
        return result

    # Fast-forward merge if possible
    merge_base = run(
        ["git", "merge-base", "HEAD", "upstream/main"],
        check=False, cwd=WORKSPACE_GATEWAY_REPO,
    )
    merge_base_rev = merge_base.stdout.strip() if merge_base.returncode == 0 else ""
    can_ff = False
    if merge_base_rev:
        ff_check = run(
            ["git", "rev-list", "--count", f"{merge_base_rev}..HEAD"],
            check=False, cwd=WORKSPACE_GATEWAY_REPO,
        )
        local_ahead = int(ff_check.stdout.strip()) if ff_check.returncode == 0 and ff_check.stdout.strip().isdigit() else None
        can_ff = local_ahead == 0

    if can_ff:
        ff = run(["git", "merge", "--ff-only", "upstream/main"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
        result["rebase_stdout"] = ff.stdout[-4000:]
        result["rebase_stderr"] = ff.stderr[-4000:]
        if ff.returncode == 0:
            result["after_rev"] = repo_head(WORKSPACE_GATEWAY_REPO)
            result["rebased"] = True
            result["strategy"] = "fast-forward"
            push = run(["git", "push", "origin", result["branch"], "--force-with-lease"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
            result["push_stdout"] = push.stdout[-2000:]
            result["push_stderr"] = push.stderr[-2000:]
            if push.returncode != 0:
                result["error"] = "push_failed"
                result["warning"] = "Fast-forwarded but push failed — next patch sync will use local commit only"
                return result
            result["pushed"] = True
            return result
        result["ff_attempt_failed"] = True

    # Rebase with autostash
    rebase = run(
        ["git", "rebase", "upstream/main", "--autostash"],
        check=False, cwd=WORKSPACE_GATEWAY_REPO,
    )
    result["rebase_stdout"] = rebase.stdout[-4000:]
    result["rebase_stderr"] = rebase.stderr[-4000:]
    if rebase.returncode != 0:
        result["error"] = "rebase_failed"
        result["strategy"] = "rebase-autostash"

        checkout_theirs = run(
            ["git", "checkout", "--theirs", "uv.lock"],
            check=False, cwd=WORKSPACE_GATEWAY_REPO,
        )
        if checkout_theirs.returncode == 0:
            git_add = run(["git", "add", "uv.lock"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
            continue_rebase = run(
                ["git", "rebase", "--continue"],
                check=False, cwd=WORKSPACE_GATEWAY_REPO,
            )
            if continue_rebase.returncode == 0:
                result.pop("error", None)
                result["auto_resolved"] = ["uv.lock"]
                result["after_rev"] = repo_head(WORKSPACE_GATEWAY_REPO)
                result["rebased"] = True
                result["strategy"] = "rebase-autostash-uv.lock-theirs"
                push = run(
                    ["git", "push", "origin", result["branch"], "--force-with-lease"],
                    check=False, cwd=WORKSPACE_GATEWAY_REPO,
                )
                result["push_stdout"] = push.stdout[-2000:]
                result["push_stderr"] = push.stderr[-2000:]
                result["pushed"] = push.returncode == 0
                if push.returncode != 0:
                    result["warning"] = "Rebased and auto-resolved but push failed"
                return result

        run(["git", "rebase", "--abort"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
        return result

    result["after_rev"] = repo_head(WORKSPACE_GATEWAY_REPO)
    result["rebased"] = True
    result["strategy"] = "rebase-autostash"

    push = run(["git", "push", "origin", result["branch"], "--force-with-lease"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
    result["push_stdout"] = push.stdout[-2000:]
    result["push_stderr"] = push.stderr[-2000:]
    if push.returncode != 0:
        result["error"] = "push_failed"
        result["warning"] = "Rebased but push failed — next patch sync will use local commit only"
        return result

    result["pushed"] = True
    return result


def sync_patch_set(dry_run: bool) -> dict[str, Any]:
    """Extract bug-fix patches from the fork against upstream/main and write to patches/."""
    result: dict[str, Any] = {
        "component": "patch-sync",
        "workspace_repo": str(WORKSPACE_GATEWAY_REPO),
        "patches_dir": str(PATCHES_DIR),
        "enabled": True,
        "synced": False,
        "needs_sync": False,
        "patch_files": [],
        "errors": [],
    }
    if not DOCKERFILE_GATEWAY.exists():
        result["error"] = "dockerfile_missing"
        return result

    if not (WORKSPACE_GATEWAY_REPO / ".git").exists():
        result["error"] = "workspace_not_a_git_repo"
        return result

    current_rev = repo_head(WORKSPACE_GATEWAY_REPO)
    result["workspace_branch"] = repo_branch(WORKSPACE_GATEWAY_REPO)
    result["workspace_rev"] = current_rev
    result["workspace_status"] = repo_status(WORKSPACE_GATEWAY_REPO)

    if result["workspace_status"]:
        result["error"] = "patch_source_dirty"
        return result

    # Detect whether we need to regenerate patches
    PATCHES_DIR.mkdir(exist_ok=True)
    existing_patches = sorted(PATCHES_DIR.glob("patch_*.diff"))
    result["existing_patches"] = [p.name for p in existing_patches]

    # Read current fork HEAD to detect changes
    fetch_w = run(["git", "fetch", "origin", "--quiet"], check=False, cwd=WORKSPACE_GATEWAY_REPO)
    origin_rev = run(["git", "rev-parse", "origin/main"], check=False, cwd=WORKSPACE_GATEWAY_REPO).stdout.strip()
    result["origin_rev"] = origin_rev

    # Compute patches against upstream/main for each tracked file
    all_patch_content: list[tuple[str, str]] = []
    for filename in PATCH_SOURCE_FILES:
        diff_proc = run(
            ["git", "diff", "upstream/main..HEAD", "--", filename],
            check=False,
            cwd=WORKSPACE_GATEWAY_REPO,
        )
        if diff_proc.stdout.strip():
            patch_name = filename.replace("/", "_").replace(".", "_") + ".diff"
            all_patch_content.append((patch_name, diff_proc.stdout))
        else:
            # Patch is identical to upstream — remove any stale patch file
            stale = PATCHES_DIR / f"patch_{filename.replace('/', '_').replace('.', '_')}.diff"
            if stale.exists():
                stale.unlink()

    result["computed_patches"] = len(all_patch_content)
    result["needs_sync"] = (
        origin_rev != current_rev
        or not existing_patches
        or any(
            (PATCHES_DIR / name).read_text() != content
            for name, content in all_patch_content
        )
    )

    if dry_run or not result["needs_sync"]:
        return result

    # Write new patches
    written: list[str] = []
    for patch_name, content in all_patch_content:
        patch_path = PATCHES_DIR / patch_name
        patch_path.write_text(content)
        written.append(patch_name)

    # Remove patches that are no longer needed (file matches upstream now)
    for existing in existing_patches:
        if existing.name not in [name for name, _ in all_patch_content]:
            existing.unlink()
            result.setdefault("removed_patches", []).append(existing.name)

    result["patch_files"] = written
    result["synced"] = True
    return result


def inspect_host_mcp_tool(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return result
    text = path.read_text()
    result["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return result


def inspect_container_mcp_tool(container: str, path: str, exec_user: str) -> dict[str, Any]:
    script = (
        "from pathlib import Path\n"
        "import hashlib, json, sys\n"
        "path = Path(sys.argv[1])\n"
        "result = {'path': str(path), 'exists': path.exists()}\n"
        "if path.exists():\n"
        "    text = path.read_text()\n"
        "    result['sha256'] = hashlib.sha256(text.encode()).hexdigest()\n"
        "print(json.dumps(result))\n"
    )
    proc = run([
        "docker", "exec", "--user", exec_user, container, "python3", "-c", script, path
    ], check=False, cwd=None)
    result: dict[str, Any] = {"path": path, "exists": False, "returncode": proc.returncode}
    if proc.stdout.strip():
        try:
            result.update(json.loads(proc.stdout.strip()))
        except json.JSONDecodeError:
            result["stdout"] = proc.stdout[-2000:]
    if proc.stderr.strip():
        result["stderr"] = proc.stderr[-2000:]
    return result


def hashes_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(left.get("exists") and right.get("exists") and left.get("sha256") and left.get("sha256") == right.get("sha256"))


def verify_mcp_runtime(specs: dict[str, dict[str, Any]]) -> list[str]:
    source = inspect_host_mcp_tool(WORKSPACE_MCP_TOOL)
    if not source.get("exists"):
        return ["workspace source mcp_tool.py missing"]

    errors: list[str] = []
    webui = inspect_container_mcp_tool(specs["hermes-webui"]["container"], WEBUI_RUNTIME_MCP_TOOL, "1000")
    if not hashes_match(source, webui):
        errors.append("hermes-webui: mcp_tool runtime stale")

    for service in gateway_services(specs):
        info = inspect_container_mcp_tool(specs[service]["container"], GATEWAY_RUNTIME_MCP_TOOL, "1000:1000")
        if not hashes_match(source, info):
            errors.append(f"{service}: mcp_tool runtime stale")

    return errors


def sync_workspace_to_build(dry_run: bool) -> dict[str, Any]:
    """Sync rebased workspace/hermes-agent-private into build/hermes-agent-private.

    The WebUI container mounts ./build/hermes-agent-private:/workspace/hermes-agent-private,
    while the gateway mounts ./workspace:/workspace. After a rebase the workspace has
    new commits but the build directory is stale unless we rsync.
    """
    result: dict[str, Any] = {
        "component": "workspace-to-build-sync",
        "workspace": str(WORKSPACE_GATEWAY_REPO),
        "build": str(BUILD_WEBUI_REPO),
        "synced": False,
    }
    if not WORKSPACE_GATEWAY_REPO.exists():
        result["error"] = "workspace_missing"
        return result
    if not BUILD_WEBUI_REPO.exists():
        result["error"] = "build_missing"
        return result

    # Quick check: compare HEAD commits
    ws_head = repo_head(WORKSPACE_GATEWAY_REPO)
    build_head = repo_head(BUILD_WEBUI_REPO)
    result["workspace_head"] = ws_head
    result["build_head"] = build_head

    if ws_head == build_head:
        result["synced"] = True
        result["note"] = "already in sync"
        return result

    if dry_run:
        result["note"] = f"would sync {ws_head[:8]} into build"
        return result

    # Use rsync to mirror workspace into build, preserving .git and mode
    rsync = run(
        [
            "rsync", "-a", "--delete",
            "--exclude=.git/refs/remotes/origin/HEAD",
            str(WORKSPACE_GATEWAY_REPO) + "/",
            str(BUILD_WEBUI_REPO) + "/",
        ],
        check=False, cwd=STACK_DIR,
    )
    result["rsync_stdout"] = rsync.stdout[-2000:]
    result["rsync_stderr"] = rsync.stderr[-2000:]
    if rsync.returncode != 0:
        result["error"] = "rsync_failed"
        return result

    # Verify
    after_head = repo_head(BUILD_WEBUI_REPO)
    result["after_head"] = after_head
    result["synced"] = after_head == ws_head
    if not result["synced"]:
        result["error"] = "head_mismatch_after_sync"
    return result


def patch_dockerfile_webui() -> dict[str, Any]:
    """Ensure Dockerfile.webui does not hardcode a stale upstream USER.

    Upstream changed the runtime user from hermeswebuitoo → hermeswebui and
    the init script now requires starting as root so it can adjust UID/GID.
    A hardcoded 'USER hermeswebuitoo' at the end of Dockerfile.webui breaks
    the container. We remove any USER line so the container starts as root.
    """
    result: dict[str, Any] = {
        "component": "dockerfile-webui-patch",
        "dockerfile": str(DOCKERFILE_WEBUI),
        "patched": False,
    }
    if not DOCKERFILE_WEBUI.exists():
        result["error"] = "dockerfile_missing"
        return result

    text = DOCKERFILE_WEBUI.read_text()
    lines = text.splitlines()
    new_lines: list[str] = []
    removed: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("USER hermeswebuitoo") or stripped.startswith("USER hermeswebui"):
            removed.append(stripped)
            continue
        new_lines.append(line)

    if not removed:
        result["note"] = "no stale USER line found"
        result["patched"] = True
        return result

    new_text = "\n".join(new_lines) + "\n"
    DOCKERFILE_WEBUI.write_text(new_text)
    result["removed"] = removed
    result["patched"] = True
    return result


def clear_webui_deps_marker(container: str) -> dict[str, Any]:
    proc = run(["docker", "exec", "--user", "0", container, "rm", "-f", "/app/venv/.deps_installed"], check=False, cwd=None)
    result: dict[str, Any] = {"container": container, "returncode": proc.returncode}
    if proc.stdout.strip():
        result["stdout"] = proc.stdout[-2000:]
    if proc.stderr.strip():
        result["stderr"] = proc.stderr[-2000:]
    return result


def verification_targets(changed: set[str], specs: dict[str, dict[str, Any]]) -> list[str]:
    targets = set(changed)
    gateway_set = set(gateway_services(specs))
    if changed & gateway_set:
        targets.update(gateway_set)
    if "tailscale" in changed:
        targets.update(specs.keys())
    return [service for service in specs.keys() if service in targets]


def workspace_mcp_changed(specs: dict[str, dict[str, Any]]) -> bool:
    """Return True if workspace mcp_tool.py differs from any gateway runtime."""
    source = inspect_host_mcp_tool(WORKSPACE_MCP_TOOL)
    if not source.get("exists"):
        return False
    for service in gateway_services(specs):
        info = inspect_container_mcp_tool(specs[service]["container"], GATEWAY_RUNTIME_MCP_TOOL, "1000:1000")
        if not hashes_match(source, info):
            return True
    return False


def verify_stack(specs: dict[str, dict[str, Any]]) -> tuple[bool, list[str]]:
    deadline = time.time() + VERIFY_TIMEOUT
    last_errors: list[str] = []
    tail_container = specs["tailscale"]["container"]

    time.sleep(20)

    while time.time() < deadline:
        errors: list[str] = []
        tail_id_proc = run(["docker", "inspect", tail_container, "--format", "{{.Id}}"], check=False)
        tailscale_id = tail_id_proc.stdout.strip() if tail_id_proc.returncode == 0 else ""
        if not tailscale_id:
            errors.append("tailscale: container missing")
        for service, spec in specs.items():
            container = spec["container"]
            proc = run(["docker", "inspect", container, "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"], check=False)
            if proc.returncode != 0:
                errors.append(f"{service}: container missing")
                continue
            status = proc.stdout.strip()
            if spec["healthcheck"] == "healthy":
                if "running healthy" not in status:
                    errors.append(f"{service}: {status}")
            elif spec["healthcheck"] == "running":
                if not status.startswith("running"):
                    errors.append(f"{service}: {status}")
            if service != "tailscale" and tailscale_id:
                network_mode_proc = run(["docker", "inspect", container, "--format", "{{.HostConfig.NetworkMode}}"], check=False)
                network_mode = network_mode_proc.stdout.strip() if network_mode_proc.returncode == 0 else ""
                expected_mode = f"container:{tailscale_id}"
                if network_mode != expected_mode:
                    errors.append(f"{service}: stale tailscale namespace ({network_mode or 'missing'} != {expected_mode})")

        web = run(["docker", "exec", tail_container, "sh", "-c", "wget -qO- http://127.0.0.1:8787/health"], check=False)
        if web.returncode != 0 or '"status": "ok"' not in web.stdout:
            errors.append("webui health endpoint failed")

        for service in runtime_services(specs):
            container = specs[service]["container"]
            if service == "hermes-webui":
                exec_user = "1000"
                python_path = "/app/venv/bin/python"
            else:
                exec_user = "1000:1000"
                python_path = "/opt/hermes/.venv/bin/python"
            git_proc = run(["docker", "exec", "--user", exec_user, container, "git", "--version"], check=False)
            croniter_proc = run([
                "docker", "exec", "--user", exec_user, container, python_path, "-c",
                "import croniter; print('croniter import OK')",
            ], check=False)
            if git_proc.returncode != 0:
                errors.append(f"{service}: git missing")
            if croniter_proc.returncode != 0:
                errors.append(f"{service}: croniter missing")
            # Check gh auth in both hermes-webui and gateways (both now have gh CLI installed)
            gh_proc = run(["docker", "exec", "--user", exec_user, container, "gh", "auth", "status"], check=False)
            if gh_proc.returncode != 0:
                errors.append(f"{service}: gh auth unavailable")

        errors.extend(verify_mcp_runtime(specs))

        if not errors:
            return True, []
        last_errors = errors
        time.sleep(10)

    return False, last_errors


def get_recent_logs(container: str, lines: int = 120) -> str:
    proc = run(["docker", "logs", "--tail", str(lines), container], check=False)
    return proc.stdout + proc.stderr


def dispatch_agenthub_task(summary: dict[str, Any]) -> dict[str, Any]:
    """Dispatch an AgentHub delegation to the infrastructure profile when safe updates fail."""
    result: dict[str, Any] = {"component": "agenthub-dispatch", "dispatched": False}
    status = summary.get("status", "unknown")
    errors = summary.get("errors", [])
    changed = summary.get("changed", [])
    rolled_back = summary.get("rolled_back", False)
    rollback_verified = summary.get("rollback_verified", False)

    failure_statuses = {
        "service_discovery_failed",
        "patch_sync_failed",
        "auth_sync_failed",
        "pull_failed",
        "build_failed",
        "verification_failed_no_updates",
        "rollback_failed",
        "rolled_back",
    }
    if status not in failure_statuses and not errors:
        result["skipped"] = True
        result["reason"] = f"status={status} does not require infrastructure dispatch"
        return result

    context_parts = [
        f"Safe update failed with status: {status}",
    ]
    if errors:
        context_parts.append(f"Errors: {errors}")
    if changed:
        changed_services = [c.get("service", "unknown") for c in changed]
        context_parts.append(f"Changed services: {changed_services}")
    if rolled_back:
        context_parts.append(f"Rolled back: {rolled_back}, rollback verified: {rollback_verified}")
    if summary.get("log_snippets"):
        for svc, snippet in summary["log_snippets"].items():
            if snippet:
                context_parts.append(f"{svc} logs (last 200 chars): ...{snippet[-200:]}")

    context = "\n".join(context_parts)

    title = f"Infrastructure alert: safe update {status} on hermes-webui-stack"
    goal = (
        "Investigate and remediate the safe update failure on the hermes-webui-stack. "
        "Check service health, image state, and compose configuration. "
        "Apply fixes and verify the stack is healthy before closing this task."
    )

    shell_cmd = (
        "cd /workspace/AgentHub && "
        "PYTHONPATH=/workspace/hermes-agent-private:/workspace/AgentHub "
        "python3 scripts/delegate_profile_task.py submit "
        "--sender-profile personal "
        "--recipient-profile infrastructure "
        + f'--title "{title}" '
        + f'--goal "{goal}" '
        + f'--context-summary "{context}" '
        + '--success-criteria-json \'["Identify root cause", "Apply fix or escalate", "Verify stack health", "Report findings"]\' '
        + "--priority high "
        + "--profile-root /opt/data/profiles "
        + "--artifact-root /workspace/llm-wikis/profiles"
    )

    cmd = ["docker", "exec", "hermes-webui-stack-gateway", "sh", "-lc", shell_cmd]
    proc = run(cmd, check=False, cwd=None)
    result["stdout"] = proc.stdout[-4000:]
    result["stderr"] = proc.stderr[-4000:]
    result["returncode"] = proc.returncode

    if proc.returncode == 0:
        try:
            payload = json.loads(proc.stdout.strip())
            result.update(payload)
            result["dispatched"] = True
        except Exception:
            result["dispatch_parse_error"] = "no_json_found"
            result["dispatched"] = True
    else:
        result["error"] = "agenthub_dispatch_command_failed"

    return result


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    summary: dict[str, Any] = {
        "stack_dir": str(STACK_DIR),
        "dry_run": dry_run,
        "changed": [],
        "updated_targets": [],
        "verified": False,
        "rolled_back": False,
        "rollback_verified": False,
        "status": "unknown",
        "errors": [],
        "log_snippets": {},
    }

    try:
        specs = discover_service_specs()
    except Exception as exc:
        summary["status"] = "service_discovery_failed"
        summary["errors"].append(str(exc))
        summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
        print(json.dumps(summary, indent=2))
        return 1

    summary["discovered_services"] = list(specs.keys())
    summary["gateway_services"] = gateway_services(specs)

    # Preflight: check compose fingerprint drift
    try:
        import subprocess
        drift_result = subprocess.run(
            [sys.executable or "python3", str(STACK_DIR / "scripts" / "check_compose_fingerprint.py")],
            capture_output=True, text=True, timeout=30, cwd=str(STACK_DIR)
        )
        summary["compose_fingerprint"] = {
            "exit_code": drift_result.returncode,
            "stdout": drift_result.stdout.strip(),
            "stderr": drift_result.stderr.strip(),
        }
        if drift_result.returncode != 0:
            summary.setdefault("warnings", []).append(
                "Compose fingerprint drift detected. Run `python3 scripts/check_compose_fingerprint.py --fix` "
                "to acknowledge the changed state before rebuilding."
            )
    except Exception as exc:
        summary.setdefault("warnings", []).append(f"compose_fingerprint_check_failed: {exc}")

    # Step 1: rebase fork onto upstream
    summary["upstream_rebase"] = rebase_workspace_from_upstream(dry_run=dry_run)
    rebase_error = summary["upstream_rebase"].get("error")
    if rebase_error:
        summary.setdefault("warnings", []).append(f"upstream_rebase: {rebase_error}")
        # If we are behind upstream and rebase failed, this is not a safe "no updates"
        # situation — the fork is stale and needs manual conflict resolution.
        behind = summary["upstream_rebase"].get("behind", 0)
        if behind > 0 and rebase_error == "rebase_failed":
            summary["status"] = "rebase_failed"
            summary["errors"].append(
                f"workspace rebase failed ({behind} commits behind upstream/main). "
                "Manual conflict resolution required."
            )
            summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
            print(json.dumps(summary, indent=2))
            return 1

    # Step 1b: sync rebased workspace into build/ so WebUI sees new commits
    summary["workspace_to_build_sync"] = sync_workspace_to_build(dry_run=dry_run)
    if summary["workspace_to_build_sync"].get("error"):
        summary.setdefault("warnings", []).append(f"workspace_to_build_sync: {summary['workspace_to_build_sync']['error']}")

    # Step 1c: fix Dockerfile.webui if upstream user name changed
    summary["dockerfile_webui_patch"] = patch_dockerfile_webui()
    if summary["dockerfile_webui_patch"].get("error"):
        summary.setdefault("warnings", []).append(f"dockerfile_webui_patch: {summary['dockerfile_webui_patch']['error']}")

    # Step 2: extract patches from rebased fork
    summary["patch_sync"] = sync_patch_set(dry_run=dry_run)
    if summary["patch_sync"].get("error"):
        summary["status"] = "patch_sync_failed"
        summary["errors"].append(summary["patch_sync"]["error"])
        summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
        print(json.dumps(summary, indent=2))
        return 1

    summary["auth_sync"] = sync_host_github_auth()
    if summary["auth_sync"].get("error"):
        summary["status"] = "auth_sync_failed"
        summary["errors"].append("host GitHub auth sync failed")
        summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
        print(json.dumps(summary, indent=2))
        return 1

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for service, spec in specs.items():
        running_id = get_container_image_id(spec["container"])
        before[service] = {
            "running_image_id": running_id,
            "running_image": get_image_meta(running_id),
            "tag_image_id": get_image_id(spec["image"]),
            "tag_image": get_image_meta(spec["image"]),
        }

    if dry_run:
        summary["status"] = "dry_run"
        summary["before"] = before
        print(json.dumps(summary, indent=2))
        return 0

    pull_targets = list(dict.fromkeys(spec["pull_ref"] for spec in specs.values()))
    pull_ok, pull_stdout, pull_stderr = pull_refs(pull_targets)
    summary["pull_stdout"] = pull_stdout[-12000:]
    summary["pull_stderr"] = pull_stderr[-12000:]
    if not pull_ok:
        summary["status"] = "pull_failed"
        summary["errors"].append("docker pull failed")
        summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
        print(json.dumps(summary, indent=2))
        return 1

    buildable_services = [service for service, spec in specs.items() if spec.get("build")]
    if buildable_services:
        # Pass upstream image version as build arg so Dockerfile.webui can
        # patch api/_version.py with the correct version string.
        webui_version = ""
        if "hermes-webui" in specs:
            webui_meta = get_image_meta(specs["hermes-webui"]["pull_ref"])
            webui_version = (webui_meta.get("labels") or {}).get("org.opencontainers.image.version") or ""
        build_cmd = ["docker", "compose", "build", "--pull"]
        if webui_version:
            build_cmd.extend(["--build-arg", f"WEBUI_VERSION={webui_version}"])
            summary["build_webui_version_arg"] = webui_version
        build_cmd.extend(buildable_services)
        build_proc = run(build_cmd, check=False)
        summary["build_stdout"] = build_proc.stdout[-12000:]
        summary["build_stderr"] = build_proc.stderr[-12000:]
        if build_proc.returncode != 0:
            summary["status"] = "build_failed"
            summary["errors"].append("docker compose build failed")
            summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
            print(json.dumps(summary, indent=2))
            return 1

    after_build: dict[str, Any] = {}
    for service, spec in specs.items():
        running_id = get_container_image_id(spec["container"])
        tag_id = get_image_id(spec["image"])
        after_build[service] = {
            "running_image_id": running_id,
            "running_image": get_image_meta(running_id),
            "tag_image_id": tag_id,
            "tag_image": get_image_meta(spec["image"]),
            "pull_ref_image": get_image_meta(spec["pull_ref"]),
        }

    changed_services: set[str] = set()
    for service, spec in specs.items():
        running_before = before[service]["running_image_id"]
        built_tag = after_build[service]["tag_image_id"]
        if running_before and built_tag and running_before != built_tag:
            changed_services.add(service)
            summary["changed"].append({
                "service": service,
                "image": spec["image"],
                "pull_ref": spec["pull_ref"],
                "old": before[service]["running_image"],
                "new": after_build[service]["tag_image"],
                "upstream": after_build[service]["pull_ref_image"],
            })

    # Restart gateway containers when workspace was rebased (no image change needed,
    # but the bind-mounted /workspace/hermes-agent-private has new commits).
    if not changed_services:
        rebased = summary.get("upstream_rebase", {}).get("rebased") or False
        if rebased:
            summary["webui_refresh"] = clear_webui_deps_marker(specs["hermes-webui"]["container"])

            recreate_proc = run(["docker", "compose", "up", "-d", "--force-recreate", "hermes-webui"], check=False)
            summary["recreate_stdout"] = recreate_proc.stdout[-4000:]
            summary["recreate_stderr"] = recreate_proc.stderr[-4000:]
            summary["recreated_services"] = ["hermes-webui"]

            gateway_restart = gateway_services(specs)
            summary["restarted_services"] = gateway_restart
            if gateway_restart:
                # Use --force-recreate (not restart) so stale Python bytecode
                # and runtime state are fully cleared when workspace code changes.
                restart_proc = run(
                    ["docker", "compose", "up", "-d", "--force-recreate", *gateway_restart],
                    check=False,
                )
                summary["restart_stdout"] = restart_proc.stdout[-4000:]
                summary["restart_stderr"] = restart_proc.stderr[-4000:]
                if restart_proc.returncode != 0:
                    summary.setdefault("warnings", []).append(
                        f"workspace rebased but recreate of {gateway_restart} failed; recreate manually"
                    )

            if recreate_proc.returncode != 0:
                summary["status"] = "workspace_rebased_recreate_failed"
                summary["errors"].append("docker compose up --force-recreate hermes-webui failed")
                summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
                print(json.dumps(summary, indent=2))
                return 1

            ok, restart_errors = verify_stack(specs)
            summary["verified"] = ok
            summary["errors"].extend(restart_errors)
            if not ok:
                summary["status"] = "restart_verification_failed"
            else:
                summary["status"] = "workspace_rebased_recreated"
            print(json.dumps(summary, indent=2))
            return 0

        summary["verified"] = True
        summary["status"] = "no_updates"
        print(json.dumps(summary, indent=2))
        return 0

    targets = verification_targets(changed_services, specs)
    if workspace_mcp_changed(specs):
        targets.extend(gateway_services(specs))
        targets = list(dict.fromkeys(targets))  # dedupe, preserve order
    summary["updated_targets"] = targets
    if "hermes-webui" in targets:
        summary["webui_refresh"] = clear_webui_deps_marker(specs["hermes-webui"]["container"])
    up_proc = run(["docker", "compose", "up", "-d", "--force-recreate", *targets], check=False)
    summary["update_stdout"] = up_proc.stdout[-12000:]
    summary["update_stderr"] = up_proc.stderr[-12000:]

    ok, errors = verify_stack(specs)
    summary["verified"] = ok
    summary["errors"].extend(errors)
    if ok:
        summary["status"] = "updated"
        print(json.dumps(summary, indent=2))
        return 0

    rollback_errors: list[str] = []
    rollback_image_ids: dict[str, str] = {}
    for service in changed_services:
        image_ref = specs[service]["image"]
        old_id = before[service]["running_image_id"]
        if old_id and image_ref not in rollback_image_ids:
            rollback_image_ids[image_ref] = old_id
    for image_ref in sorted({specs[service]["image"] for service in changed_services}):
        old_id = rollback_image_ids.get(image_ref)
        if not old_id:
            rollback_errors.append(f"{image_ref}: missing old image id for rollback")
            continue
        tag_proc = run(["docker", "tag", old_id, image_ref], check=False)
        if tag_proc.returncode != 0:
            rollback_errors.append(f"{image_ref}: failed to retag old image")
    summary["rolled_back"] = True
    if not rollback_errors:
        rb_targets = verification_targets(changed_services, specs)
        rb_proc = run(["docker", "compose", "up", "-d", "--force-recreate", *rb_targets], check=False)
        summary["rollback_stdout"] = rb_proc.stdout[-12000:]
        summary["rollback_stderr"] = rb_proc.stderr[-12000:]
        rb_ok, rb_verify_errors = verify_stack(specs)
        summary["rollback_verified"] = rb_ok
        rollback_errors.extend(rb_verify_errors)
    summary["errors"].extend(rollback_errors)
    for service in specs:
        summary["log_snippets"][service] = get_recent_logs(specs[service]["container"], 120)[-8000:]
    summary["status"] = "rolled_back" if summary["rollback_verified"] else "rollback_failed"
    summary["agenthub_dispatch"] = dispatch_agenthub_task(summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["rollback_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
