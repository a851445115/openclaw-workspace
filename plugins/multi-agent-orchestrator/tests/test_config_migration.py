import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MIGRATE = REPO / "scripts" / "migrate-config-v2"


def _run_json(cmd):
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _policy_path(self) -> Path:
        return self.root / "config" / "runtime-policy.json"

    def _write_policy(self, payload):
        self._policy_path().write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def test_old_shape_migration_supports_dry_run_and_apply(self):
        old = {
            "agents": ["coder", "debugger"],
            "orchestrator": {"maxConcurrentSpawns": 2},
        }
        self._write_policy(old)
        before = self._policy_path().read_text(encoding="utf-8")

        dry = _run_json(
            [
                str(MIGRATE),
                "--root",
                str(self.root),
                "--dry-run",
            ]
        )
        self.assertTrue(dry.get("ok"), dry)
        self.assertTrue(dry.get("changed"), dry)
        self.assertTrue(dry.get("wouldWrite"), dry)
        self.assertFalse(dry.get("applied"), dry)
        self.assertEqual(self._policy_path().read_text(encoding="utf-8"), before, dry)

        apply_out = _run_json(
            [
                str(MIGRATE),
                "--root",
                str(self.root),
                "--apply",
            ]
        )
        self.assertTrue(apply_out.get("ok"), apply_out)
        self.assertTrue(apply_out.get("changed"), apply_out)
        self.assertTrue(apply_out.get("applied"), apply_out)
        migrated = json.loads(self._policy_path().read_text(encoding="utf-8"))
        self.assertEqual(
            migrated.get("agents"),
            [{"id": "coder", "capabilities": []}, {"id": "debugger", "capabilities": []}],
            migrated,
        )
        orchestrator = migrated.get("orchestrator") if isinstance(migrated.get("orchestrator"), dict) else {}
        self.assertEqual(orchestrator.get("maxConcurrentSpawns"), 2, migrated)
        self.assertIn("retryPolicy", orchestrator, migrated)
        self.assertIn("budgetPolicy", orchestrator, migrated)

    def test_mixed_shape_migration_keeps_explicit_values(self):
        mixed = {
            "agents": [
                "coder",
                {"id": "debugger", "capabilities": ["triage", "logs"]},
            ],
            "orchestrator": {
                "retryPolicy": {"maxAttempts": 4},
                "budgetPolicy": {"guardrails": {"maxTaskTokens": 999}},
            },
        }
        self._write_policy(mixed)

        out = _run_json([str(MIGRATE), "--root", str(self.root), "--apply"])
        self.assertTrue(out.get("ok"), out)
        migrated = json.loads(self._policy_path().read_text(encoding="utf-8"))
        self.assertEqual(
            migrated.get("agents"),
            [{"id": "coder", "capabilities": []}, {"id": "debugger", "capabilities": ["triage", "logs"]}],
            migrated,
        )
        orchestrator = migrated.get("orchestrator") if isinstance(migrated.get("orchestrator"), dict) else {}
        retry = orchestrator.get("retryPolicy") if isinstance(orchestrator.get("retryPolicy"), dict) else {}
        budget = orchestrator.get("budgetPolicy") if isinstance(orchestrator.get("budgetPolicy"), dict) else {}
        guardrails = budget.get("guardrails") if isinstance(budget.get("guardrails"), dict) else {}
        self.assertEqual(retry.get("maxAttempts"), 4, migrated)
        self.assertEqual(guardrails.get("maxTaskTokens"), 999, migrated)
        self.assertGreaterEqual(int(guardrails.get("maxTaskRetries") or 0), 1, migrated)

    def test_v2full_is_noop_and_apply_is_idempotent(self):
        full = {
            "agents": [
                {"id": "coder", "capabilities": ["code", "tests"]},
                {"id": "invest-analyst", "capabilities": ["research", "citations"]},
            ],
            "orchestrator": {
                "maxConcurrentSpawns": 6,
                "retryPolicy": {
                    "maxAttempts": 5,
                    "backoff": {
                        "mode": "exponential",
                        "baseMs": 200,
                        "maxMs": 2000,
                        "multiplier": 2.5,
                        "jitterPct": 10,
                    },
                },
                "budgetPolicy": {
                    "guardrails": {
                        "maxTaskTokens": 3210,
                        "maxTaskWallTimeSec": 456,
                        "maxTaskRetries": 3,
                    }
                },
            },
        }
        self._write_policy(full)

        dry = _run_json([str(MIGRATE), "--root", str(self.root), "--dry-run"])
        self.assertTrue(dry.get("ok"), dry)
        self.assertFalse(dry.get("changed"), dry)
        self.assertFalse(dry.get("applied"), dry)

        apply_once = _run_json([str(MIGRATE), "--root", str(self.root), "--apply"])
        self.assertTrue(apply_once.get("ok"), apply_once)
        self.assertFalse(apply_once.get("changed"), apply_once)
        self.assertFalse(apply_once.get("applied"), apply_once)

        apply_twice = _run_json([str(MIGRATE), "--root", str(self.root), "--apply"])
        self.assertTrue(apply_twice.get("ok"), apply_twice)
        self.assertFalse(apply_twice.get("changed"), apply_twice)
        self.assertFalse(apply_twice.get("applied"), apply_twice)
        self.assertEqual(json.loads(self._policy_path().read_text(encoding="utf-8")), full, apply_twice)

    def test_apply_writes_backup_once_then_remains_stable(self):
        old = {
            "agents": ["coder"],
            "orchestrator": {"maxConcurrentSpawns": 1},
        }
        self._write_policy(old)
        backup_dir = self.root / "state" / "config-migration-backups"

        first = _run_json([str(MIGRATE), "--root", str(self.root), "--apply"])
        self.assertTrue(first.get("ok"), first)
        self.assertTrue(first.get("changed"), first)
        backups_after_first = sorted(backup_dir.glob("runtime-policy.*.json"))
        self.assertEqual(len(backups_after_first), 1, first)

        second = _run_json([str(MIGRATE), "--root", str(self.root), "--apply"])
        self.assertTrue(second.get("ok"), second)
        self.assertFalse(second.get("changed"), second)
        backups_after_second = sorted(backup_dir.glob("runtime-policy.*.json"))
        self.assertEqual(len(backups_after_second), 1, second)


if __name__ == "__main__":
    unittest.main()
