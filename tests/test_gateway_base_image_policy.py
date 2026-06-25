import importlib.util
from pathlib import Path
from types import SimpleNamespace


STACK_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = STACK_DIR / "scripts" / "stack_upgrade_v2.py"
LEGACY_SAFE_UPDATE = STACK_DIR / "scripts" / "safe_update_stack.py"
DOCKERFILE_GATEWAY = STACK_DIR / "Dockerfile.gateway"
STACK_ENTRYPOINT = STACK_DIR / "scripts" / "gateway-stack-entrypoint.sh"
DOCKERIGNORE = STACK_DIR / ".dockerignore"
EXPECTED_GATEWAY_BASE = "nousresearch/hermes-agent:main"
RETIRED_RELEASE_BASE = "nousresearch/hermes-agent:latest"
EXPECTED_STACK_ENTRYPOINT = '["/usr/bin/tini", "-g", "--", "/opt/hermes/docker/stack-entrypoint.sh"]'


def load_stack_upgrade_module():
    spec = importlib.util.spec_from_file_location("stack_upgrade_v2_policy_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stack_upgrade_pulls_gateway_from_main_branch_image():
    module = load_stack_upgrade_module()

    assert module.GATEWAY_PULL_REF == EXPECTED_GATEWAY_BASE


def test_gateway_dockerfile_uses_main_branch_base_image():
    first_non_comment = next(
        line.strip()
        for line in DOCKERFILE_GATEWAY.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert first_non_comment == f"FROM {EXPECTED_GATEWAY_BASE}"


def test_active_gateway_update_paths_do_not_reference_release_latest_base():
    active_policy_files = [DOCKERFILE_GATEWAY, SCRIPT_PATH, LEGACY_SAFE_UPDATE]

    offenders = [str(path.relative_to(STACK_DIR)) for path in active_policy_files if RETIRED_RELEASE_BASE in path.read_text()]

    assert offenders == []


def test_gateway_dockerfile_installs_tini_for_compose_entrypoints():
    dockerfile = DOCKERFILE_GATEWAY.read_text()

    assert "tini" in dockerfile
    assert "apt-get install -y --no-install-recommends" in dockerfile


def test_gateway_dockerfile_preserves_stack_entrypoint_contract():
    dockerfile = DOCKERFILE_GATEWAY.read_text()

    assert STACK_ENTRYPOINT.exists()
    assert "COPY scripts/gateway-stack-entrypoint.sh /opt/hermes/docker/stack-entrypoint.sh" in dockerfile
    assert "chmod +x /opt/hermes/docker/stack-entrypoint.sh" in dockerfile
    assert f"ENTRYPOINT {EXPECTED_STACK_ENTRYPOINT}" in dockerfile



def test_dockerignore_allows_stack_entrypoint_in_build_context():
    lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

    assert "*" in lines
    assert "!scripts/" in lines
    assert "!scripts/gateway-stack-entrypoint.sh" in lines

def test_candidate_runtime_dependency_check_blocks_missing_tini():
    module = load_stack_upgrade_module()
    calls = []

    def fake_run(cmd, timeout=None, cwd=None, check=False):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="missing tini\n")

    module.run = fake_run
    report = {"errors": [], "warnings": []}
    result = module.verify_candidate_runtime_dependencies(
        report,
        "candidate-image",
        required_paths=["/usr/bin/tini"],
        smoke_user=None,
    )

    assert result["ok"] is False
    assert result["checks"][0]["path"] == "/usr/bin/tini"
    assert result["checks"][0]["returncode"] == 1
    assert report["errors"][0]["code"] == "candidate_runtime_dependency_missing"
    assert calls == [["docker", "run", "--rm", "--entrypoint", "/bin/sh", "candidate-image", "-lc", "test -x /usr/bin/tini"]]


def test_candidate_runtime_dependency_check_runs_non_root_default_entrypoint_smoke():
    module = load_stack_upgrade_module()
    calls = []

    def fake_run(cmd, timeout=None, cwd=None, check=False):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="Hermes Agent v0.test\n", stderr="")

    module.run = fake_run
    report = {"errors": [], "warnings": []}
    result = module.verify_candidate_runtime_dependencies(
        report,
        "candidate-image",
        required_paths=["/usr/bin/tini"],
        smoke_user="1000:1000",
    )

    assert result["ok"] is True
    assert result["entrypoint_smoke"]["returncode"] == 0
    assert result["entrypoint_smoke"]["user"] == "1000:1000"
    assert result["entrypoint_smoke"]["cmd"] == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/opt/data:uid=1000,gid=1000,mode=1770",
        "candidate-image",
        "--version",
    ]
    assert calls[-1] == result["entrypoint_smoke"]["cmd"]
    assert report["errors"] == []


def test_candidate_runtime_dependency_check_blocks_non_root_entrypoint_smoke_failure():
    module = load_stack_upgrade_module()

    def fake_run(cmd, timeout=None, cwd=None, check=False):
        if "--entrypoint" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=111, stdout="", stderr="s6-applyuidgid failed\n")

    module.run = fake_run
    report = {"errors": [], "warnings": []}
    result = module.verify_candidate_runtime_dependencies(
        report,
        "candidate-image",
        required_paths=["/usr/bin/tini"],
        smoke_user="1000:1000",
    )

    assert result["ok"] is False
    assert result["entrypoint_smoke"]["returncode"] == 111
    assert report["errors"][0]["code"] == "candidate_entrypoint_smoke_failed"
