import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
INIT = SCRIPTS / "init-task-board"


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_task(self, task_id: str, assignee: str, title: str):
        return run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            f"@{assignee} create task {task_id}: {title}",
        ])

    def _dispatch(self, task_id: str, agent: str, spawn_output: str):
        return run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            task_id,
            "--agent",
            agent,
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            spawn_output,
        ])

    def test_reason_codes_enter_recovery_chain(self):
        self._create_task("T-101", "coder", "spawn failed branch")
        out1 = self._dispatch("T-101", "coder", '{"status":"failed","message":"worker crashed"}')
        self.assertEqual(out1["spawn"]["reasonCode"], "spawn_failed", out1)
        self.assertEqual(out1["spawn"]["action"], "retry", out1)
        self.assertEqual(out1["spawn"]["attempt"], 1, out1)
        self.assertEqual(out1["spawn"]["nextAssignee"], "debugger", out1)

        self._create_task("T-102", "debugger", "incomplete output branch")
        out2 = self._dispatch("T-102", "debugger", '{"status":"done","summary":"done"}')
        self.assertEqual(out2["spawn"]["reasonCode"], "incomplete_output", out2)
        self.assertEqual(out2["spawn"]["action"], "retry", out2)
        self.assertEqual(out2["spawn"]["attempt"], 1, out2)
        self.assertEqual(out2["spawn"]["nextAssignee"], "invest-analyst", out2)

        self._create_task("T-103", "invest-analyst", "blocked signal branch")
        out3 = self._dispatch("T-103", "invest-analyst", '{"message":"[BLOCKED] waiting for upstream data"}')
        self.assertEqual(out3["spawn"]["reasonCode"], "blocked_signal", out3)
        self.assertEqual(out3["spawn"]["action"], "human", out3)
        self.assertEqual(out3["spawn"]["attempt"], 1, out3)
        self.assertEqual(out3["spawn"]["nextAssignee"], "human", out3)

    def test_cooldown_prevents_attempt_increment(self):
        self._create_task("T-110", "coder", "cooldown branch")
        first = self._dispatch("T-110", "coder", '{"status":"failed","message":"first fail"}')
        second = self._dispatch("T-110", "coder", '{"status":"failed","message":"second fail"}')

        self.assertEqual(first["spawn"]["attempt"], 1, first)
        self.assertEqual(second["spawn"]["attempt"], 1, second)
        self.assertTrue(second["spawn"]["cooldownActive"], second)
        self.assertEqual(second["spawn"]["action"], "retry", second)
        self.assertEqual(second["spawn"]["nextAssignee"], "debugger", second)

    def test_over_budget_escalates_to_human(self):
        policy_path = self.root / "config" / "recovery-policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            json.dumps(
                {
                    "recoveryChain": ["coder", "debugger", "invest-analyst", "human"],
                    "default": {"maxAttempts": 1, "cooldownSec": 0},
                    "reasonPolicies": {
                        "spawn_failed": {"maxAttempts": 1, "cooldownSec": 0},
                        "incomplete_output": {"maxAttempts": 1, "cooldownSec": 0},
                        "blocked_signal": {"maxAttempts": 1, "cooldownSec": 0},
                    },
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        self._create_task("T-120", "coder", "budget branch")
        first = self._dispatch("T-120", "coder", '{"status":"failed","message":"first fail"}')
        second = self._dispatch("T-120", "coder", '{"status":"failed","message":"second fail"}')

        self.assertEqual(first["spawn"]["action"], "retry", first)
        self.assertEqual(first["spawn"]["attempt"], 1, first)

        self.assertEqual(second["spawn"]["action"], "escalate", second)
        self.assertEqual(second["spawn"]["nextAssignee"], "human", second)
        self.assertEqual(second["spawn"]["recoveryState"], "escalated_to_human", second)

    def test_autopilot_returns_recovery_fields(self):
        self._create_task("T-130", "coder", "autopilot recovery branch")
        out = run_json([
            "python3",
            str(MILE),
            "autopilot",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--spawn",
            "--max-steps",
            "1",
            "--spawn-output",
            '{"status":"failed","message":"worker crashed"}',
        ])

        self.assertEqual(out["stepsRun"], 1, out)
        spawn = ((out.get("steps") or [{}])[0].get("dispatch") or {}).get("spawn") or {}
        self.assertEqual(spawn.get("reasonCode"), "spawn_failed", out)
        self.assertEqual(spawn.get("action"), "retry", out)
        self.assertEqual(spawn.get("attempt"), 1, out)
        self.assertEqual(spawn.get("nextAssignee"), "debugger", out)


if __name__ == "__main__":
    unittest.main()
