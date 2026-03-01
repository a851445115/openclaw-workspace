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


class BudgetGovernanceTests(unittest.TestCase):
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

    def _write_policy(self, max_tokens: int, max_time_sec: int, max_retries: int):
        policy_path = self.root / "config" / "budget-policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            json.dumps(
                {
                    "global": {
                        "maxTaskTokens": max_tokens,
                        "maxTaskWallTimeSec": max_time_sec,
                        "maxTaskRetries": max_retries,
                        "degradePolicy": ["reduced_context", "manual_handoff", "stop_run"],
                        "onExceeded": "manual_handoff",
                    },
                    "agents": {
                        "coder": {
                            "degradePolicy": ["manual_handoff", "stop_run"],
                        }
                    },
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_token_budget_exceeded_blocks_and_escalates_human(self):
        self._write_policy(max_tokens=50, max_time_sec=3600, max_retries=3)
        self._create_task("T-B601", "coder", "token budget")
        out = self._dispatch(
            "T-B601",
            "coder",
            '{"status":"done","summary":"已完成","evidence":["pytest passed"],"metrics":{"tokenUsage":120,"elapsedMs":500}}',
        )

        spawn = out["spawn"]
        self.assertEqual(spawn["reasonCode"], "budget_exceeded", out)
        self.assertEqual(spawn["decision"], "blocked", out)
        self.assertEqual(spawn["nextAssignee"], "human", out)
        self.assertEqual(spawn["action"], "escalate", out)
        self.assertEqual(spawn["degradeAction"], "manual_handoff", out)
        self.assertIn("maxTaskTokens", spawn.get("exceededKeys") or [], out)

    def test_time_budget_exceeded_blocks_and_escalates_human(self):
        self._write_policy(max_tokens=1000, max_time_sec=1, max_retries=3)
        self._create_task("T-B602", "coder", "time budget")
        out = self._dispatch(
            "T-B602",
            "coder",
            '{"status":"done","summary":"已完成","evidence":["logs/run.log"],"metrics":{"tokenUsage":9,"elapsedMs":2500}}',
        )

        spawn = out["spawn"]
        self.assertEqual(spawn["reasonCode"], "budget_exceeded", out)
        self.assertEqual(spawn["decision"], "blocked", out)
        self.assertEqual(spawn["nextAssignee"], "human", out)
        self.assertEqual(spawn["action"], "escalate", out)
        self.assertIn("maxTaskWallTimeSec", spawn.get("exceededKeys") or [], out)

    def test_retry_budget_precheck_blocks_before_spawn(self):
        self._write_policy(max_tokens=1000, max_time_sec=3600, max_retries=1)
        self._create_task("T-B603", "coder", "retry budget")
        state_path = self.root / "state" / "budget.state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "entries": {
                        "T-B603|coder": {
                            "taskId": "T-B603",
                            "agent": "coder",
                            "tokenUsage": 10,
                            "elapsedMs": 500,
                            "retryCount": 1,
                        }
                    },
                    "updatedAt": "2026-03-01T00:00:00Z",
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        out = self._dispatch(
            "T-B603",
            "coder",
            '{"status":"done","summary":"已完成","evidence":["logs/retry.log"],"metrics":{"tokenUsage":10,"elapsedMs":100}}',
        )

        spawn = out["spawn"]
        self.assertEqual(spawn["reasonCode"], "budget_exceeded", out)
        self.assertEqual(spawn["decision"], "blocked", out)
        self.assertEqual(spawn["nextAssignee"], "human", out)
        self.assertEqual(spawn["action"], "escalate", out)
        self.assertIn("maxTaskRetries", spawn.get("exceededKeys") or [], out)

    def test_usage_alias_fields_do_not_double_count_or_trigger_budget_exceeded(self):
        self._write_policy(max_tokens=80, max_time_sec=3600, max_retries=3)
        self._create_task("T-B604", "coder", "usage alias budget")
        out = self._dispatch(
            "T-B604",
            "coder",
            (
                '{"status":"done","summary":"已完成","evidence":["pytest passed"],'
                '"usage":{"prompt_tokens":25,"completion_tokens":25,"input_tokens":25,"output_tokens":25}}'
            ),
        )

        spawn = out["spawn"]
        self.assertEqual((spawn.get("metrics") or {}).get("tokenUsage"), 50, out)
        self.assertNotEqual(spawn.get("reasonCode"), "budget_exceeded", out)
        self.assertEqual(spawn["decision"], "done", out)


if __name__ == "__main__":
    unittest.main()
