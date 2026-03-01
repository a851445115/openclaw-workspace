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


def run_json(cmd, cwd=REPO, expect_success=True):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if expect_success and proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        payload = json.loads((proc.stdout or "").strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc.returncode, payload


class GovernanceControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_task(self, task_id: str, assignee: str = "coder", title: str = "治理测试任务") -> None:
        _, out = run_json(
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
        self.assertTrue(out.get("ok"), out)

    def _dispatch(self, task_id: str, expect_success=True):
        return run_json(
            [
                "python3",
                str(MILE),
                "dispatch",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--task-id",
                task_id,
                "--agent",
                "coder",
                "--mode",
                "dry-run",
            ],
            expect_success=expect_success,
        )

    def _autopilot(self, expect_success=True):
        return run_json(
            [
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
                '{"status":"done","message":"已完成，证据: logs/governance.log"}',
            ],
            expect_success=expect_success,
        )

    def _scheduler_run(self, action: str = "tick", expect_success=True):
        return run_json(
            [
                "python3",
                str(MILE),
                "scheduler-run",
                "--root",
                str(self.root),
                "--action",
                action,
                "--interval-sec",
                "60",
                "--max-steps",
                "1",
                "--mode",
                "dry-run",
                "--spawn",
                "--spawn-output",
                '{"status":"done","summary":"scheduler", "evidence":["logs/scheduler.log"]}',
            ],
            expect_success=expect_success,
        )

    def _govern(self, text: str, expect_success=True):
        return run_json(
            [
                "python3",
                str(MILE),
                "feishu-router",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                text,
                "--mode",
                "dry-run",
            ],
            expect_success=expect_success,
        )

    def _status(self, task_id: str):
        _, out = run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"status {task_id}",
            ]
        )
        return out

    def _write_governance_control(self, approvals):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "paused": False,
            "frozen": False,
            "aborts": {"global": 0, "autopilot": 0, "scheduler": 0, "tasks": {}},
            "approvals": approvals,
        }
        (state_dir / "governance.control.json").write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    def _read_audit(self):
        path = self.root / "state" / "governance.audit.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def test_pause_resume_changes_behavior_and_writes_audit(self):
        self._create_task("T-801", "coder", "pause/resume")

        _, paused = self._govern("@orchestrator 治理 暂停")
        self.assertTrue(paused.get("ok"), paused)
        self.assertEqual(paused.get("intent"), "governance", paused)

        _, auto_paused = run_json(
            [
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
                '{"status":"done","message":"已完成，证据: logs/pause.log"}',
            ]
        )
        self.assertTrue(auto_paused.get("ok"), auto_paused)
        self.assertTrue(auto_paused.get("skipped"), auto_paused)
        self.assertEqual(auto_paused.get("reason"), "governance_paused", auto_paused)

        _, resumed = self._govern("@orchestrator 治理 恢复")
        self.assertTrue(resumed.get("ok"), resumed)

        _, auto_resumed = run_json(
            [
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
                '{"status":"done","message":"已完成，证据: logs/resume.log"}',
            ]
        )
        self.assertTrue(auto_resumed.get("ok"), auto_resumed)
        self.assertEqual(auto_resumed.get("stepsRun"), 1, auto_resumed)

        rows = self._read_audit()
        self.assertGreaterEqual(len(rows), 2, rows)
        actions = [str(row.get("action") or "") for row in rows]
        self.assertIn("pause", actions, rows)
        self.assertIn("resume", actions, rows)
        for row in rows:
            self.assertTrue(row.get("at"), row)
            self.assertTrue(row.get("actor"), row)
            self.assertTrue(row.get("action"), row)
            self.assertIn("result", row, row)
            self.assertTrue(row.get("hash"), row)

    def test_freeze_blocks_dispatch(self):
        self._create_task("T-802", "coder", "freeze")
        _, frozen = self._govern("@orchestrator 治理 冻结")
        self.assertTrue(frozen.get("ok"), frozen)

        _, blocked = self._dispatch("T-802", expect_success=False)
        self.assertFalse(blocked.get("ok"), blocked)
        self.assertEqual(blocked.get("reason"), "governance_frozen", blocked)

        status = self._status("T-802")
        self.assertEqual((status.get("task") or {}).get("status"), "pending", status)

    def test_freeze_blocks_autopilot_and_scheduler_with_skipped_reason(self):
        self._create_task("T-802A", "coder", "freeze autopilot scheduler")
        _, frozen = self._govern("@orchestrator 治理 冻结")
        self.assertTrue(frozen.get("ok"), frozen)

        _, auto = self._autopilot()
        self.assertTrue(auto.get("ok"), auto)
        self.assertTrue(auto.get("skipped"), auto)
        self.assertEqual(auto.get("reason"), "governance_frozen", auto)
        self.assertEqual(auto.get("stopReason"), "governance_frozen", auto)

        _, scheduler = self._scheduler_run(action="enable")
        self.assertTrue(scheduler.get("ok"), scheduler)
        self.assertTrue(scheduler.get("skipped"), scheduler)
        self.assertEqual(scheduler.get("reason"), "governance_frozen", scheduler)
        run = scheduler.get("run") or {}
        self.assertTrue(run.get("skipped"), scheduler)
        self.assertEqual(run.get("reason"), "governance_frozen", scheduler)

    def test_freeze_then_unfreeze_recovers_autopilot_and_scheduler(self):
        self._create_task("T-802B", "coder", "freeze/unfreeze recovery")
        _, frozen = self._govern("@orchestrator 治理 冻结")
        self.assertTrue(frozen.get("ok"), frozen)

        _, auto_frozen = self._autopilot()
        self.assertEqual(auto_frozen.get("reason"), "governance_frozen", auto_frozen)

        _, scheduler_frozen = self._scheduler_run(action="enable")
        self.assertEqual(scheduler_frozen.get("reason"), "governance_frozen", scheduler_frozen)

        _, unfrozen = self._govern("@orchestrator 治理 解冻")
        self.assertTrue(unfrozen.get("ok"), unfrozen)

        _, auto_unfrozen = self._autopilot()
        self.assertTrue(auto_unfrozen.get("ok"), auto_unfrozen)
        self.assertNotEqual(auto_unfrozen.get("reason"), "governance_frozen", auto_unfrozen)
        self.assertNotEqual(auto_unfrozen.get("stopReason"), "governance_frozen", auto_unfrozen)

        _, scheduler_unfrozen = self._scheduler_run(action="enable")
        self.assertTrue(scheduler_unfrozen.get("ok"), scheduler_unfrozen)
        self.assertNotEqual(scheduler_unfrozen.get("reason"), "governance_frozen", scheduler_unfrozen)
        run = scheduler_unfrozen.get("run") or {}
        self.assertNotEqual(run.get("reason"), "governance_frozen", scheduler_unfrozen)

    def test_freeze_unfreeze_message_and_audit_scope_cover_runtime(self):
        _, frozen = self._govern("@orchestrator 治理 冻结")
        self.assertTrue(frozen.get("ok"), frozen)
        frozen_text = str((((frozen.get("send") or {}).get("payload") or {}).get("text") or ""))
        self.assertEqual(frozen_text, "[TASK] 治理已冻结：dispatch、自动推进与调度已阻断。", frozen)

        _, unfrozen = self._govern("@orchestrator 治理 解冻")
        self.assertTrue(unfrozen.get("ok"), unfrozen)
        unfrozen_text = str((((unfrozen.get("send") or {}).get("payload") or {}).get("text") or ""))
        self.assertEqual(unfrozen_text, "[TASK] 治理已解冻：dispatch、自动推进与调度可继续。", unfrozen)

        rows = self._read_audit()
        freeze_rows = [row for row in rows if str(row.get("action") or "") == "freeze"]
        unfreeze_rows = [row for row in rows if str(row.get("action") or "") == "unfreeze"]
        self.assertTrue(freeze_rows, rows)
        self.assertTrue(unfreeze_rows, rows)
        self.assertEqual((freeze_rows[-1].get("target") or {}).get("scope"), "dispatch/autopilot/scheduler", freeze_rows[-1])
        self.assertEqual((unfreeze_rows[-1].get("target") or {}).get("scope"), "dispatch/autopilot/scheduler", unfreeze_rows[-1])

    def test_abort_task_hits_once_and_is_consumed(self):
        self._create_task("T-803", "coder", "abort task")
        _, aborted = self._govern("@orchestrator 治理 中止 T-803")
        self.assertTrue(aborted.get("ok"), aborted)

        _, first = self._dispatch("T-803", expect_success=False)
        self.assertFalse(first.get("ok"), first)
        self.assertEqual(first.get("reason"), "governance_aborted", first)

        _, second = self._dispatch("T-803", expect_success=True)
        self.assertTrue(second.get("ok"), second)

    def test_dispatch_requires_approval_then_allows_after_approve(self):
        self._create_task("T-804", "coder", "approval")
        self._write_governance_control(
            {
                "APR-900": {
                    "id": "APR-900",
                    "status": "pending",
                    "target": {"type": "dispatch", "taskId": "T-804"},
                }
            }
        )

        _, waiting = self._dispatch("T-804", expect_success=False)
        self.assertFalse(waiting.get("ok"), waiting)
        self.assertEqual(waiting.get("reason"), "approval_required", waiting)
        self.assertEqual(waiting.get("approvalId"), "APR-900", waiting)

        _, approved = self._govern("@orchestrator 治理 审批 通过 APR-900")
        self.assertTrue(approved.get("ok"), approved)

        _, passed = self._dispatch("T-804", expect_success=True)
        self.assertTrue(passed.get("ok"), passed)

    def test_approval_rejected_blocks_dispatch(self):
        self._create_task("T-805", "coder", "approval rejected")
        self._write_governance_control(
            {
                "APR-901": {
                    "id": "APR-901",
                    "status": "rejected",
                    "target": {"type": "dispatch", "taskId": "T-805"},
                }
            }
        )

        _, blocked = self._dispatch("T-805", expect_success=False)
        self.assertFalse(blocked.get("ok"), blocked)
        self.assertEqual(blocked.get("reason"), "approval_rejected", blocked)
        self.assertEqual(blocked.get("approvalId"), "APR-901", blocked)

    def test_dispatch_approval_agent_match_is_case_insensitive(self):
        self._create_task("T-806", "coder", "approval case-insensitive agent")
        self._write_governance_control(
            {
                "APR-902": {
                    "id": "APR-902",
                    "status": "pending",
                    "target": {"type": "dispatch", "taskId": "T-806", "agent": "CoDeR"},
                }
            }
        )

        _, blocked = self._dispatch("T-806", expect_success=False)
        self.assertFalse(blocked.get("ok"), blocked)
        self.assertEqual(blocked.get("reason"), "approval_required", blocked)
        self.assertEqual(blocked.get("approvalId"), "APR-902", blocked)

    def test_feishu_router_reaches_chinese_governance_commands(self):
        self._write_governance_control(
            {
                "APR-PASS": {"id": "APR-PASS", "status": "pending", "target": {"type": "dispatch", "taskId": "T-999"}},
                "APR-REJECT": {"id": "APR-REJECT", "status": "pending", "target": {"type": "dispatch", "taskId": "T-999"}},
            }
        )
        cases = [
            ("@orchestrator 治理 状态", "status"),
            ("@orchestrator 治理 暂停", "pause"),
            ("@orchestrator 治理 恢复", "resume"),
            ("@orchestrator 治理 冻结", "freeze"),
            ("@orchestrator 治理 解冻", "unfreeze"),
            ("@orchestrator 治理 中止 调度", "abort"),
            ("@orchestrator 治理 中止 自动推进", "abort"),
            ("@orchestrator 治理 中止 全部", "abort"),
            ("@orchestrator 治理 审批 通过 APR-PASS", "approve"),
            ("@orchestrator 治理 审批 拒绝 APR-REJECT", "reject"),
        ]

        for text, action in cases:
            with self.subTest(text=text):
                _, out = self._govern(text)
                self.assertTrue(out.get("ok"), out)
                self.assertEqual(out.get("intent"), "governance", out)
                self.assertEqual((out.get("governance") or {}).get("action"), action, out)


if __name__ == "__main__":
    unittest.main()
