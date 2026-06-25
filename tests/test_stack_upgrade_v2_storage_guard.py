import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "stack_upgrade_v2.py"


def load_stack_upgrade_module():
    spec = importlib.util.spec_from_file_location("stack_upgrade_v2_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_backup_storage_guard_blocks_stack_local_backup_dir(tmp_path):
    module = load_stack_upgrade_module()
    stack = tmp_path / "stack"
    stack.mkdir()
    backup_dir = stack / "backups"
    backup_dir.mkdir()
    expected = tmp_path / "docker-runtime" / "backups"
    expected.mkdir(parents=True)

    module.STACK_DIR = stack
    module.BACKUP_ROOT = backup_dir / "upgrade-v2"
    module.EXPECTED_BACKUP_STORAGE_ROOT = expected

    report = {"errors": [], "warnings": []}
    module.check_backup_storage(report)

    assert any(err["code"] == "backup_root_not_on_expected_storage" for err in report["errors"])
    assert report["backup_storage"]["backup_path"] == str(backup_dir.resolve())


def test_backup_storage_guard_accepts_symlink_to_expected_storage(tmp_path):
    module = load_stack_upgrade_module()
    stack = tmp_path / "stack"
    stack.mkdir()
    expected = tmp_path / "docker-runtime" / "backups"
    expected.mkdir(parents=True)
    (stack / "backups").symlink_to(expected, target_is_directory=True)

    module.STACK_DIR = stack
    module.BACKUP_ROOT = stack / "backups" / "upgrade-v2"
    module.EXPECTED_BACKUP_STORAGE_ROOT = expected

    report = {"errors": [], "warnings": []}
    module.check_backup_storage(report)

    assert report["errors"] == []
    assert report["backup_storage"]["backup_path"] == str(expected.resolve())


def test_prune_upgrade_backups_keeps_newest_timestamped_sets(tmp_path):
    module = load_stack_upgrade_module()
    backup_root = tmp_path / "upgrade-v2"
    backup_root.mkdir()
    timestamps = [
        "20260521T200630Z",
        "20260521T202050Z",
        "20260522T092548Z",
        "20260525T023251Z",
        "20260525T055349Z",
        "20260525T092114Z",
        "20260525T181553Z",
        "20260525T181616Z",
        "20260525T201930Z",
    ]
    for ts in timestamps:
        d = backup_root / ts
        d.mkdir()
        (d / "manifest.json").write_text("{}\n")
    (backup_root / "not-a-timestamp").mkdir()

    result = module.prune_upgrade_backups(backup_root=backup_root, keep=5, dry_run=False)

    assert result["removed_count"] == 4
    assert [p.name for p in sorted(backup_root.iterdir())] == [
        "20260525T055349Z",
        "20260525T092114Z",
        "20260525T181553Z",
        "20260525T181616Z",
        "20260525T201930Z",
        "not-a-timestamp",
    ]

def test_patch_dockerignore_for_private_source(tmp_path):
    """_patch_dockerignore_for_private_source adds negation when build/ is excluded."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sut", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    stack = tmp_path / "stack"
    stack.mkdir()
    module.STACK_DIR = stack

    # Case 1: .dockerignore has blanket build/ exclusion
    di = stack / ".dockerignore"
    di.write_text("build/\nnode_modules/\n.env\n")

    with module._patch_dockerignore_for_private_source():
        content = di.read_text()
        assert "!build/hermes-agent-private/" in content
        assert content.strip().endswith("!build/hermes-agent-private/")
    # After exit, original is restored
    assert "!build/hermes-agent-private/" not in di.read_text()
    assert "build/" in di.read_text()

    # Case 2: no build/ exclusion — no patching needed
    di.write_text("node_modules/\n.env\n")
    with module._patch_dockerignore_for_private_source():
        assert di.read_text() == "node_modules/\n.env\n"

    # Case 3: negation already present — idempotent
    di.write_text("build/\n!build/hermes-agent-private/\n")
    original = di.read_text()
    with module._patch_dockerignore_for_private_source():
        assert di.read_text() == original

    # Case 4: no .dockerignore file — no crash
    di.unlink()
    with module._patch_dockerignore_for_private_source():
        pass  # should not raise


def test_verify_candidate_manifest_checks_image_ids(monkeypatch):
    module = load_stack_upgrade_module()
    manifest = {
        "schema": "hermes-webui-stack.candidate.v1",
        "status": "ok",
        "gateway_candidate": "local/gateway:candidate",
        "webui_candidate": "local/webui:candidate",
        "gateway_image_id": "sha256:gateway",
        "webui_image_id": "sha256:webui",
    }

    def fake_image_id(ref):
        return {
            "local/gateway:candidate": "sha256:gateway",
            "local/webui:candidate": "sha256:webui",
        }.get(ref, "")

    monkeypatch.setattr(module, "image_id", fake_image_id)
    assert module.verify_candidate_manifest(manifest) == []

    manifest["webui_image_id"] = "sha256:wrong"
    errors = module.verify_candidate_manifest(manifest)
    assert any(error["code"] == "candidate_image_id_mismatch" for error in errors)
