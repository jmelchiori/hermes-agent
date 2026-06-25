import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts import skill_maintenance_lane as lane


def write_skill(root: Path, rel: str, content: str = "---\nname: sample\n---\n\n# Sample\n") -> Path:
    skill_dir = root / "skills" / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


class SkillMaintenanceReportTests(unittest.TestCase):
    def test_report_classifies_hub_local_modification_as_manual_review(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            skill_dir = write_skill(home, "devops/docker-management", "local edit\n")
            lock_hash = "sha256:notlivehash"
            lock = {
                "version": 1,
                "installed": {
                    "docker-management": {
                        "source": "official",
                        "identifier": "official/devops/docker-management",
                        "trust_level": "builtin",
                        "scan_verdict": "caution",
                        "content_hash": lock_hash,
                        "install_path": "devops/docker-management",
                        "files": ["SKILL.md"],
                    }
                },
            }
            hub = home / "skills" / ".hub"
            hub.mkdir(parents=True)
            (hub / "lock.json").write_text(json.dumps(lock))

            profile = lane.inspect_profile(
                lane.ProfileSpec(name="developer", home=home, apply_allowed=True),
                update_rows=[{
                    "name": "docker-management",
                    "identifier": "official/devops/docker-management",
                    "source": "official",
                    "status": "update_available",
                    "current_hash": lock_hash,
                    "latest_hash": "sha256:latest",
                }],
                now="2026-05-22T05:00:00Z",
            )

            finding = profile["findings"][0]
            self.assertEqual(finding["finding"], "hub_locally_modified_and_upstream_update_available")
            self.assertEqual(finding["classification"], "manual_review")
            self.assertEqual(finding["recommended_decision"], "rebase_local_edits_onto_upstream")
            self.assertEqual(finding["live_hash"], lane.hub_content_hash(skill_dir))

    def test_report_marks_unmodified_official_hub_update_as_auto_apply_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            skill_dir = write_skill(home, "security/sherlock", "same as lock\n")
            live_hash = lane.hub_content_hash(skill_dir)
            lock = {
                "version": 1,
                "installed": {
                    "sherlock": {
                        "source": "official",
                        "identifier": "official/security/sherlock",
                        "trust_level": "builtin",
                        "scan_verdict": "caution",
                        "content_hash": live_hash,
                        "install_path": "security/sherlock",
                        "files": ["SKILL.md"],
                    }
                },
            }
            hub = home / "skills" / ".hub"
            hub.mkdir(parents=True)
            (hub / "lock.json").write_text(json.dumps(lock))

            profile = lane.inspect_profile(
                lane.ProfileSpec(name="developer", home=home, apply_allowed=True),
                update_rows=[{
                    "name": "sherlock",
                    "identifier": "official/security/sherlock",
                    "source": "official",
                    "status": "update_available",
                    "current_hash": live_hash,
                    "latest_hash": "sha256:latest",
                }],
                now="2026-05-22T05:00:00Z",
            )

            finding = profile["findings"][0]
            self.assertEqual(finding["finding"], "hub_update_available")
            self.assertEqual(finding["classification"], "auto_apply_ok")
            self.assertEqual(finding["recommended_decision"], "update_allowlisted_official_skill")

    def test_report_detects_bundled_user_modified_skill(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            skill_dir = write_skill(home, "devops/local-bundled", "changed\n")
            manifest = {"local-bundled": "original-md5"}
            (home / "skills" / ".bundled_manifest").write_text(json.dumps(manifest))

            profile = lane.inspect_profile(
                lane.ProfileSpec(name="developer", home=home, apply_allowed=True),
                update_rows=[],
                now="2026-05-22T05:00:00Z",
            )

            finding = profile["findings"][0]
            self.assertEqual(finding["finding"], "bundled_skill_user_modified")
            self.assertEqual(finding["classification"], "manual_review")
            self.assertEqual(finding["live_hash"], lane.bundled_dir_hash(skill_dir))




    def test_plan_contains_only_auto_apply_candidates_and_verified_backups(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            write_skill(home, "security/sherlock", "old sherlock\n")
            report = {
                "generated_at": "2026-05-22T05:00:00Z",
                "status": "ok",
                "profiles": [{
                    "profile": "developer",
                    "home": "/opt/data/profiles/developer",
                    "host_home": str(home),
                    "findings": [
                        {
                            "profile": "developer",
                            "home": "/opt/data/profiles/developer",
                            "host_home": str(home),
                            "skill": "sherlock",
                            "path": "security/sherlock",
                            "provenance": "hub:official",
                            "finding": "hub_update_available",
                            "classification": "auto_apply_ok",
                            "recommended_decision": "update_allowlisted_official_skill",
                            "lock_hash": "sha256:old",
                            "live_hash": "sha256:old",
                            "latest_hash": "sha256:new",
                            "source": "official",
                            "update_status": "update_available",
                        },
                        {
                            "profile": "developer",
                            "home": "/opt/data/profiles/developer",
                            "host_home": str(home),
                            "skill": "docker-management",
                            "path": "devops/docker-management",
                            "provenance": "hub:official",
                            "finding": "hub_locally_modified_and_upstream_update_available",
                            "classification": "manual_review",
                            "recommended_decision": "rebase_local_edits_onto_upstream",
                        },
                    ],
                }],
            }

            plan = lane.build_plan(report=report, now="2026-05-22T05:00:00Z", state_dir=root / "state", backup_dir=root / "backups")

            self.assertEqual(plan["status"], "ok")
            self.assertEqual(plan["summary"]["planned_updates"], 1)
            self.assertEqual(plan["updates"][0]["skill"], "sherlock")
            self.assertTrue(Path(plan["updates"][0]["backup"]["archive"]).exists())
            self.assertTrue(plan["updates"][0]["backup"]["verified"])
            self.assertIn("skills/security/sherlock/SKILL.md", plan["updates"][0]["backup"]["members"])

    def test_apply_requires_confirmation_and_refuses_stale_live_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            skill_dir = write_skill(home, "security/sherlock", "locally changed\n")
            live_hash = lane.hub_content_hash(skill_dir)
            plan = {
                "status": "ok",
                "plan_id": "20260522T050000Z",
                "updates": [{
                    "profile": "developer",
                    "home": "/opt/data/profiles/developer",
                    "host_home": str(home),
                    "skill": "sherlock",
                    "path": "security/sherlock",
                    "expected_live_hash": "sha256:different",
                    "expected_latest_hash": "sha256:new",
                    "backup": {"archive": str(root / "missing.tar.zst"), "verified": True},
                }],
            }

            no_confirm = lane.apply_plan(plan, confirm="WRONG", dry_run=True, state_dir=root / "state", backup_dir=root / "backups")
            self.assertEqual(no_confirm["status"], "blocked")
            self.assertIn("confirmation", no_confirm["errors"][0]["code"])

            stale = lane.apply_plan(plan, confirm="UPDATE-HERMES-SKILLS", dry_run=True, state_dir=root / "state", backup_dir=root / "backups")
            self.assertEqual(stale["status"], "blocked")
            self.assertEqual(stale["updates"][0]["status"], "blocked")
            self.assertEqual(stale["updates"][0]["live_hash"], live_hash)

    def test_rollback_restores_profile_skills_from_verified_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            write_skill(home, "security/sherlock", "before\n")
            backup = lane.create_profile_backup(
                lane.ProfileSpec(name="developer", home=home, apply_allowed=True, container_home="/opt/data/profiles/developer"),
                backup_id="20260522T050000Z",
                backup_root=root / "backups",
                reason="unit-test",
            )
            (home / "skills" / "security" / "sherlock" / "SKILL.md").write_text("after\n")

            result = lane.rollback_backup(
                backup_id="20260522T050000Z",
                profile="developer",
                confirm="RESTORE-HERMES-SKILLS",
                backup_root=root / "backups",
                profile_home=home,
                state_dir=root / "state",
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual((home / "skills" / "security" / "sherlock" / "SKILL.md").read_text(), "before\n")
            self.assertEqual(result["backup"]["archive"], backup["archive"])

    def test_docker_update_probe_pins_gateway_workdir_to_private_source_tree(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        with mock.patch.object(lane.subprocess, "run", side_effect=fake_run):
            rows, err = lane.docker_update_probe(
                lane.ProfileSpec(name="developer", home=Path("/tmp/home"), apply_allowed=True, container_home="/opt/data/profiles/developer")
            )

        self.assertEqual(rows, [])
        self.assertIsNone(err)
        self.assertIn("-w", captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("-w") + 1], "/workspace/hermes-agent-private")

    def test_bundled_manifest_parser_accepts_line_format(self):
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td) / "skills"
            skills_dir.mkdir()
            (skills_dir / ".bundled_manifest").write_text("alpha:abc123\nbeta:def456\n")

            manifest, errors = lane.load_bundled_manifest(skills_dir)

            self.assertEqual(errors, [])
            self.assertEqual(manifest, {"alpha": "abc123", "beta": "def456"})

    def test_decision_record_has_stable_delivery_fields(self):
        finding = {
            "profile": "developer",
            "home": "/opt/data/profiles/developer",
            "skill": "docker-management",
            "path": "devops/docker-management",
            "provenance": "hub:official",
            "finding": "hub_locally_modified_and_upstream_update_available",
            "classification": "manual_review",
            "recommended_decision": "rebase_local_edits_onto_upstream",
            "risk": "medium",
            "rationale": "local edits would be clobbered",
        }

        decision = lane.make_decision_record(finding, now="2026-05-22T05:00:00Z")

        self.assertTrue(decision["id"].startswith("skilldec-20260522-developer-docker-management-"))
        self.assertEqual(decision["delivery"], ["vault", "discord:#friday"])
        self.assertEqual(decision["status"], "open")
        self.assertEqual(decision["owner"], "developer")


if __name__ == "__main__":
    unittest.main()
