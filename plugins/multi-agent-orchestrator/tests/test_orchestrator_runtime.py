import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
INIT = SCRIPTS / "init-task-board"
REBUILD = SCRIPTS / "rebuild-snapshot"
RECOVER = SCRIPTS / "recover-stale-locks"
INBOUND = SCRIPTS / "feishu-inbound-router"


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatch_spawn_closes_task_done(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-001: 完成闭环",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-001",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","message":"T-001 已完成，证据: logs/run.log"}',
        ])
        self.assertTrue(dispatch["ok"], dispatch)
        self.assertTrue(dispatch["autoClose"], dispatch)
        self.assertEqual(dispatch["spawn"]["decision"], "done", dispatch)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-001",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_dispatch_spawn_done_without_evidence_is_blocked(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@debugger create task T-005: 证据门禁测试",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-005",
            "--agent",
            "debugger",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","message":"我已经定位到问题，接下来会修复"}',
        ])
        self.assertTrue(dispatch["ok"], dispatch)
        self.assertEqual(dispatch["spawn"]["decision"], "blocked", dispatch)
        self.assertEqual(dispatch["spawn"]["reasonCode"], "missing_evidence", dispatch)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-005",
        ])
        self.assertEqual(status["task"]["status"], "blocked", status)

    def test_feishu_router_handles_claim_done_commands(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-002: 命令入口测试",
        ])

        claim = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator claim T-002",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(claim["ok"], claim)

        done = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator done T-002: 已完成，测试通过，证据: docs/protocol.md",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(done["ok"], done)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-002",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_clarify_global_throttle(self):
        state_file = self.root / "state" / "clarify.cooldown.json"
        now_ts = int(time.time())
        state_file.write_text(
            json.dumps(
                {
                    "entries": {
                        "oc_041146c92a9ccb403a7f4f48fb59701d:*": {
                            "ts": now_ts,
                            "at": "2026-02-28T00:00:00Z",
                            "taskId": "T-001",
                            "by": "orchestrator",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        second_proc = subprocess.run(
            [
                "python3",
                str(MILE),
                "clarify",
                "--root",
                str(self.root),
                "--task-id",
                "T-003",
                "--role",
                "debugger",
                "--question",
                "请提供错误栈",
                "--mode",
                "dry-run",
                "--state-file",
                str(state_file),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(second_proc.returncode, 0, second_proc.stdout + second_proc.stderr)
        payload = json.loads(second_proc.stdout.strip())
        self.assertTrue(payload.get("throttled"), payload)

    def test_rebuild_and_recover_scripts(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-004: rebuild",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@coder claim task T-004",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "mark done T-004: done",
        ])

        compact_out = self.root / "state" / "tasks.compacted.jsonl"
        rebuild = run_json([
            str(REBUILD),
            "--root",
            str(self.root),
            "--apply",
            "--compact-jsonl",
            str(compact_out),
        ])
        self.assertTrue(rebuild["ok"], rebuild)
        self.assertTrue(compact_out.exists(), rebuild)

        lock_dir = self.root / "state" / "locks"
        stale = lock_dir / "manual.lock"
        stale.write_text(
            json.dumps(
                {
                    "owner": "test",
                    "pid": 999999,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "createdAtTs": int(time.time()) - 3600,
                    "expiresAtTs": int(time.time()) - 1800,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        dry = run_json([str(RECOVER), "--root", str(self.root), "--dry-run"])
        self.assertTrue(any(c["path"].endswith("manual.lock") for c in dry["candidates"]), dry)

        apply = run_json([str(RECOVER), "--root", str(self.root), "--apply"])
        self.assertTrue(apply["ok"], apply)
        self.assertFalse(stale.exists(), apply)

    def test_inbound_ignores_bot_loop(self):
        out = run_json([
            "python3",
            str(INBOUND),
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "[DONE] T-888 | 状态=已完成",
            "--milestones",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["router"].get("intent"), "ignored_loop", out)

    def test_autopilot_advances_pending_tasks(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-010: 自动推进一",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-011: 自动推进二",
        ])

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
            "2",
            "--spawn-output",
            '{"status":"done","message":"已完成，测试通过，证据: logs/auto.log"}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stepsRun"], 2, out)

        t10 = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-010",
        ])
        t11 = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-011",
        ])
        self.assertEqual(t10["task"]["status"], "done", t10)
        self.assertEqual(t11["task"]["status"], "done", t11)

    def test_acceptance_policy_blocks_weak_coder_done_report(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-020: 验收策略测试",
        ])
        run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator claim T-020",
            "--mode",
            "dry-run",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator done T-020: 已完成，证据: docs/protocol.md",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-020",
        ])
        self.assertEqual(status["task"]["status"], "blocked", status)

    def test_dispatch_handoff_visible_emits_agent_report(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-030: 可见交接模式",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-030",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--visibility-mode",
            "handoff_visible",
            "--spawn-output",
            '{"status":"done","message":"已完成，pytest 通过，证据: logs/handoff.log"}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["visibilityMode"], "handoff_visible", out)
        self.assertTrue(out["workerReport"]["ok"], out)
        self.assertEqual(
            out["workerReport"]["send"]["payload"]["accountId"],
            "coder",
            out,
        )
        self.assertIn(
            "<at user_id=",
            out["workerReport"]["send"]["payload"]["text"],
            out,
        )

    def test_dispatch_prompt_includes_snapshot_history_and_schema(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040: 结构化提示词测试",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@debugger create task T-041: 阻塞示例",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "block task T-041: sample blocked",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040",
            "--agent",
            "coder",
            "--task",
            "T-040: 结构化提示词测试",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"已完成并验证","evidence":["logs/t040.log","pytest passed"]}',
        ])
        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertIn("BOARD_SNAPSHOT", prompt, out)
        self.assertIn("TASK_RECENT_HISTORY", prompt, out)
        self.assertIn("OUTPUT_SCHEMA", prompt, out)
        self.assertIn('"status": "done|blocked|progress"', prompt, out)

    def test_dispatch_structured_done_report_marks_done(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-042: 结构化回报通过",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-042",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"代码已完成，测试通过","changes":[{"path":"src/a.py","summary":"fix bug"}],"evidence":["pytest -q passed","logs/t042.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "done", out)
        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-042",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_dispatch_structured_done_without_evidence_is_blocked(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-043: 结构化回报拦截",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-043",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"已完成"}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "missing_evidence", out)
        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-043",
        ])
        self.assertEqual(status["task"]["status"], "blocked", status)

    def test_autopilot_respects_priority_and_dependencies(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-050: 低优先级",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-051: 高优先级可执行",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-052: 高优先级但依赖未完成",
        ])
        routing = self.root / "state" / "task-routing.json"
        routing.write_text(
            json.dumps(
                {
                    "priorities": {"T-050": 10, "T-051": 90, "T-052": 100},
                    "dependsOn": {"T-052": ["T-050"]},
                }
            ),
            encoding="utf-8",
        )
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
            '{"status":"done","summary":"完成","evidence":["logs/t051.log","pytest passed"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stepsRun"], 1, out)
        self.assertEqual(out["steps"][0]["taskId"], "T-051", out)

    def test_dispatch_auto_recovery_escalates_and_closes_done(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-060: 自恢复升级测试",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-060",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--auto-recover",
            "--recovery-max-attempts",
            "2",
            "--spawn-output-seq",
            '[{"status":"blocked","message":"error stack trace"},{"status":"done","summary":"已修复，测试通过","evidence":["logs/recover.log","pytest passed"]}]',
        ])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["recovery"]["applied"], out)
        self.assertEqual(out["recovery"]["agent"], "debugger", out)
        self.assertEqual(out["spawn"]["decision"], "done", out)
        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-060",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_scheduler_run_respects_debounce_guardrail(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-070: scheduler task",
        ])
        first = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--cycles",
            "1",
            "--autopilot-steps",
            "1",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/scheduler.log","pytest passed"]}',
        ])
        self.assertTrue(first["ok"], first)
        second_proc = subprocess.run(
            [
                "python3",
                str(MILE),
                "scheduler-run",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--mode",
                "dry-run",
                "--cycles",
                "1",
                "--autopilot-steps",
                "1",
                "--debounce-sec",
                "3600",
                "--spawn-output",
                '{"status":"done","summary":"完成","evidence":["logs/scheduler.log","pytest passed"]}',
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(second_proc.returncode, 0, second_proc.stdout + second_proc.stderr)
        payload = json.loads(second_proc.stdout.strip())
        self.assertTrue(payload.get("throttled"), payload)

    def test_governance_pause_and_freeze_controls_scheduler(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-080: governance pause",
        ])
        paused = run_json([
            "python3",
            str(MILE),
            "govern",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--action",
            "pause",
            "--reason",
            "maintenance",
        ])
        self.assertTrue(paused["ok"], paused)
        blocked_proc = subprocess.run(
            [
                "python3",
                str(MILE),
                "scheduler-run",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--mode",
                "dry-run",
                "--cycles",
                "1",
                "--autopilot-steps",
                "1",
                "--spawn-output",
                '{"status":"done","summary":"完成","evidence":["logs/pause.log","pytest passed"]}',
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(blocked_proc.returncode, 0, blocked_proc.stdout + blocked_proc.stderr)
        blocked_payload = json.loads(blocked_proc.stdout.strip())
        self.assertEqual(blocked_payload.get("reasonCode"), "scheduler_paused", blocked_payload)

        run_json([
            "python3",
            str(MILE),
            "govern",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--action",
            "resume",
        ])
        run_json([
            "python3",
            str(MILE),
            "govern",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--action",
            "freeze",
            "--task-id",
            "T-080",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--cycles",
            "1",
            "--autopilot-steps",
            "1",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/pause.log","pytest passed"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["cycles"][0]["autopilot"]["stepsRun"], 0, out)

    def test_quality_gate_rejects_failed_verify_command(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-090: verify command fail",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-090",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/t090.log"],"verifyCommands":[{"cmd":"python3 -c \\"import sys; sys.exit(2)\\"","expectedExit":0}]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "verify_command_failed", out)

    def test_quality_gate_accepts_successful_verify_command(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-091: verify command pass",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-091",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/t091.log"],"verifyCommands":[{"cmd":"python3 -c \\"print(123)\\"","expectedExit":0}]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "done", out)

    def test_quality_gate_rejects_weak_evidence_only(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-092: weak evidence",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-092",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["all good"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "missing_hard_evidence", out)

    def test_quality_gate_rejects_structured_schema_mismatch(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-093: schema mismatch",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-093",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","taskId":"T-XXX","agent":"coder","summary":"完成","evidence":["logs/t093.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "blocked", out)
        self.assertEqual(out["spawn"]["reasonCode"], "schema_task_mismatch", out)

    def test_autopilot_respects_step_time_budget(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-094: step budget",
        ])
        proc = subprocess.run(
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
                "--step-time-budget-sec",
                "0",
                "--spawn-output",
                '{"status":"done","summary":"ok","evidence":["logs/t094.log","pytest passed"]}',
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload.get("reasonCode"), "task_time_budget_exceeded", payload)

    def test_scheduler_budget_stop_run_enforces_cycle_cap(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-095: cycle budget one",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-096: cycle budget two",
        ])
        proc = subprocess.run(
            [
                "python3",
                str(MILE),
                "scheduler-run",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--mode",
                "dry-run",
                "--cycles",
                "2",
                "--autopilot-steps",
                "1",
                "--cycle-time-budget-sec",
                "0",
                "--budget-degrade",
                "stop_run",
                "--spawn-output",
                '{"status":"done","summary":"ok","evidence":["logs/t095.log","pytest passed"]}',
                "--spawn",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload.get("reasonCode"), "budget_cycle_exhausted", payload)

    def test_scheduler_budget_manual_handoff_degrades(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-097: degrade one",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-098: degrade two",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--cycles",
            "2",
            "--autopilot-steps",
            "1",
            "--cycle-time-budget-sec",
            "0",
            "--budget-degrade",
            "manual_handoff",
            "--spawn-output",
            '{"status":"done","summary":"ok","evidence":["logs/t097.log","pytest passed"]}',
            "--spawn",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["budget"]["degradeApplied"], "manual_handoff", out)

    def test_decompose_goal_creates_tasks_with_dependencies(self):
        decompose_output = json.dumps(
            {
                "confidence": 0.92,
                "tasks": [
                    {
                        "id": "analysis",
                        "title": "分析需求并拆分里程碑",
                        "ownerHint": "invest-analyst",
                        "priority": 95,
                    },
                    {
                        "id": "implement",
                        "title": "实现核心能力",
                        "ownerHint": "coder",
                        "dependsOn": ["analysis"],
                        "priority": 85,
                    },
                ],
            },
            ensure_ascii=False,
        )
        out = run_json([
            "python3",
            str(MILE),
            "decompose-goal",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--goal",
            "完成一次可上线交付",
            "--decompose-output",
            decompose_output,
        ])
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["pendingApproval"], out)
        self.assertEqual(out["createdCount"], 2, out)
        self.assertEqual(out["mergedCount"], 0, out)
        mapping = {item["planId"]: item["taskId"] for item in out["planTaskMap"]}
        routing_path = self.root / "state" / "task-routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        self.assertEqual(routing["dependsOn"][mapping["implement"]], [mapping["analysis"]], routing)

    def test_decompose_goal_dedupes_existing_tasks(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-110: 实现核心能力",
        ])
        decompose_output = json.dumps(
            {
                "confidence": 0.88,
                "tasks": [
                    {"id": "impl", "title": "实现核心能力", "ownerHint": "coder", "priority": 80},
                    {"id": "verify", "title": "增加回归测试", "ownerHint": "debugger", "priority": 75},
                ],
            },
            ensure_ascii=False,
        )
        out = run_json([
            "python3",
            str(MILE),
            "decompose-goal",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--goal",
            "完成核心能力并回归",
            "--decompose-output",
            decompose_output,
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["mergedCount"], 1, out)
        self.assertEqual(out["createdCount"], 1, out)
        snapshot = json.loads((self.root / "state" / "tasks.snapshot.json").read_text(encoding="utf-8"))
        titles = [str(t.get("title") or "") for t in snapshot.get("tasks", {}).values()]
        self.assertEqual(sum(1 for x in titles if x == "实现核心能力"), 1, snapshot)

    def test_decompose_goal_low_confidence_requires_approval(self):
        decompose_output = json.dumps(
            {
                "confidence": 0.30,
                "tasks": [
                    {"id": "t1", "title": "先做探索", "ownerHint": "invest-analyst"},
                    {"id": "t2", "title": "再做实现", "ownerHint": "coder", "dependsOn": ["t1"]},
                ],
            },
            ensure_ascii=False,
        )
        out = run_json([
            "python3",
            str(MILE),
            "decompose-goal",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--goal",
            "新项目探索与实现",
            "--decompose-output",
            decompose_output,
            "--min-confidence",
            "0.6",
        ])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["pendingApproval"], out)
        self.assertEqual(out["reasonCode"], "needs_approval", out)
        snapshot = json.loads((self.root / "state" / "tasks.snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(len(snapshot.get("tasks", {})), 0, snapshot)

    def test_observability_report_includes_core_metrics(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-120: obs one",
        ])
        run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--mode",
            "dry-run",
            "--cycles",
            "1",
            "--autopilot-steps",
            "1",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/obs.log","pytest passed"]}',
        ])
        out = run_json([
            "python3",
            str(MILE),
            "observability-report",
            "--root",
            str(self.root),
            "--window-sec",
            "86400",
        ])
        self.assertTrue(out["ok"], out)
        metrics = out.get("metrics", {})
        self.assertIn("throughputDone", metrics, out)
        self.assertIn("cycleSuccessRate", metrics, out)
        self.assertIn("meanCycleTimeSec", metrics, out)
        self.assertIn("recoveryRate", metrics, out)
        self.assertIn("blockReasons", metrics, out)

    def test_observability_timeline_lists_interventions(self):
        run_json([
            "python3",
            str(MILE),
            "govern",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--action",
            "pause",
            "--reason",
            "ops-check",
        ])
        run_json([
            "python3",
            str(MILE),
            "govern",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--action",
            "resume",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "observability-timeline",
            "--root",
            str(self.root),
            "--limit",
            "20",
        ])
        self.assertTrue(out["ok"], out)
        timeline = out.get("timeline", [])
        self.assertTrue(any(str(x.get("type")) == "governance_action" for x in timeline), out)

    def test_observability_export_writes_file(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-121: obs export",
        ])
        out_file = self.root / "state" / "observability.export.json"
        out = run_json([
            "python3",
            str(MILE),
            "observability-export",
            "--root",
            str(self.root),
            "--output",
            str(out_file),
            "--limit",
            "10",
        ])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out_file.exists(), out)
        payload = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertIn("report", payload, payload)
        self.assertIn("timeline", payload, payload)


if __name__ == "__main__":
    unittest.main()
