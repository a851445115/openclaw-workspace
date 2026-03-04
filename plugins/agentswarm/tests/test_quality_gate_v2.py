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


class QualityGateV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_task(self, task_id: str, assignee: str, title: str):
        return run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"@{assignee} create task {task_id}: {title}",
            ]
        )

    def _dispatch(self, task_id: str, agent: str, spawn_output: str):
        return run_json(
            [
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
            ]
        )

    def _write_acceptance_policy(self, payload):
        path = self.root / "config" / "acceptance-policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def test_done_without_hard_evidence_is_blocked(self):
        self._create_task("T-501", "coder", "missing hard evidence")
        out = self._dispatch("T-501", "coder", '{"status":"done","summary":"已完成，准备提交"}')
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "missing_hard_evidence", out)

    def test_done_summary_with_fraction_like_token_is_blocked(self):
        self._create_task("T-505", "coder", "fraction token should not count as hard evidence")
        out = self._dispatch("T-505", "coder", '{"status":"done","summary":"已完成，输出 1/2"}')
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "missing_hard_evidence", out)

    def test_done_summary_with_valid_short_path_is_accepted(self):
        self._create_task("T-507", "coder", "valid short path should count as hard evidence")
        out = self._dispatch("T-507", "coder", '{"status":"done","summary":"已完成，输出 ui/v1"}')
        self.assertEqual(out["spawn"]["decision"], "done", out)
        self.assertEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)

    def test_done_summary_with_single_char_short_path_is_blocked(self):
        self._create_task("T-508", "coder", "single-char short path should not count as hard evidence")
        out = self._dispatch("T-508", "coder", '{"status":"done","summary":"已完成，输出 a/b"}')
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "missing_hard_evidence", out)

    def test_done_summary_with_plain_verify_phrase_is_blocked(self):
        self._create_task("T-506", "coder", "plain verify phrase should not count as hard evidence")
        out = self._dispatch("T-506", "coder", '{"status":"done","summary":"已完成，验证通过"}')
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "missing_hard_evidence", out)

    def test_done_summary_with_not_passed_phrase_is_blocked(self):
        self._create_task("T-599", "coder", "not passed phrase must not be treated as done")
        out = self._dispatch("T-599", "coder", '{"status":"done","summary":"测试未通过，见 logs/t599.log"}')
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertNotEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "failure_signal_detected", out)

    def test_done_summary_with_mixed_pass_fail_result_is_blocked(self):
        self._create_task("T-601", "coder", "mixed pass/fail result must block done acceptance")
        out = self._dispatch(
            "T-601",
            "coder",
            '{"status":"done","summary":"pytest -q => 1 passed, 2 failed; logs/t601.log"}',
        )
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertNotEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "failure_signal_detected", out)

    def test_done_summary_with_pytest_failed_nodeid_is_blocked(self):
        self._create_task("T-603", "coder", "pytest FAILED nodeid must block done acceptance")
        out = self._dispatch(
            "T-603",
            "coder",
            '{"status":"done","summary":"FAILED tests/test_demo.py::test_x; logs/t603.log"}',
        )
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertNotEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "failure_signal_detected", out)

    def test_done_summary_with_traceback_signal_is_blocked(self):
        self._create_task("T-604", "coder", "traceback signal must block done acceptance")
        out = self._dispatch(
            "T-604",
            "coder",
            '{"status":"done","summary":"Traceback (most recent call last) at logs/t604.log"}',
        )
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertNotEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "failure_signal_detected", out)

    def test_done_summary_with_error_handling_context_and_passed_evidence_is_accepted(self):
        self._create_task("T-602", "coder", "error handling wording should not trigger failure signal")
        out = self._dispatch(
            "T-602",
            "coder",
            json.dumps(
                {
                    "status": "done",
                    "summary": "已完成 error handling / 异常处理优化，并完成回归验证",
                    "evidence": [
                        "logs/t602.log",
                        "pytest -q => 5 passed in 0.08s",
                    ],
                },
                ensure_ascii=False,
            ),
        )
        self.assertEqual(out["spawn"]["decision"], "done", out)
        self.assertEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertNotEqual(out["spawn"]["acceptanceReasonCode"], "failure_signal_detected", out)

    def test_done_with_hard_evidence_is_accepted(self):
        self._create_task("T-502", "coder", "has hard evidence")
        out = self._dispatch(
            "T-502",
            "coder",
            json.dumps(
                {
                    "status": "done",
                    "summary": "已完成并验证",
                    "evidence": [
                        "docs/config.md",
                        "https://example.com/runs/502",
                        "pytest -q => 3 passed in 0.05s",
                    ],
                },
                ensure_ascii=False,
            ),
        )
        self.assertEqual(out["spawn"]["decision"], "done", out)
        self.assertEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)

    def test_verify_commands_failure_blocks_done(self):
        self._write_acceptance_policy(
            {
                "global": {
                    "requireEvidence": True,
                    "verifyCommands": ['python3 -c "import sys; sys.exit(7)"'],
                }
            }
        )
        self._create_task("T-503", "coder", "verify command failure")
        out = self._dispatch(
            "T-503",
            "coder",
            '{"status":"done","summary":"已完成","evidence":["logs/t503.log","pytest -q => 2 passed"]}',
        )
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["acceptanceReasonCode"], "verify_command_failed", out)
        self.assertIn("exit=", out["spawn"]["detail"], out)

    def test_verify_commands_pass_allows_done(self):
        self._write_acceptance_policy(
            {
                "global": {
                    "requireEvidence": True,
                    "verifyCommands": [
                        {
                            "cmd": 'python3 -c "print(\'global verify ok\')"',
                            "expectExitCode": 0,
                            "timeoutSec": 3,
                        }
                    ],
                },
                "roles": {
                    "coder": {
                        "verifyCommands": ['python3 -c "print(\'role verify ok\')"'],
                    }
                },
            }
        )
        self._create_task("T-504", "coder", "verify command success")
        out = self._dispatch(
            "T-504",
            "coder",
            '{"status":"done","summary":"已完成","evidence":["logs/t504.log","pytest -q => 4 passed"]}',
        )
        self.assertEqual(out["spawn"]["decision"], "done", out)
        self.assertEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)


if __name__ == "__main__":
    unittest.main()
