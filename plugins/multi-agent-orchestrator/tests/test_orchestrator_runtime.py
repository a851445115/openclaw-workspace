import argparse
import contextlib
import json
import io
import os
import subprocess
import tempfile
import time
import unittest
import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
INIT = SCRIPTS / "init-task-board"
REBUILD = SCRIPTS / "rebuild-snapshot"
RECOVER = SCRIPTS / "recover-stale-locks"
INBOUND = SCRIPTS / "feishu-inbound-router"


def load_milestone_module():
    spec = importlib.util.spec_from_file_location("milestones_module_for_test", str(MILE))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load milestones module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class RuntimeTests(unittest.TestCase):
    XHS_STAGE_COUNT = 16

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _prepare_xhs_inputs(self, paper_id: str = "A1"):
        workflow_root = self.root / "paper-xhs-3min-workflow"
        workflow_root.mkdir(parents=True, exist_ok=True)
        pdf_path = workflow_root / f"{paper_id}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%test\n")
        return workflow_root, pdf_path

    def _bind_task_context(self, task_id: str, dispatch_prompt: str):
        state_path = self.root / "state" / "task-context-map.json"
        state = {"tasks": {}}
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8") or "{}")
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
        tasks[task_id] = {
            "projectPath": str(self.root),
            "projectName": "runtime-test",
            "dispatchPrompt": dispatch_prompt,
        }
        state["tasks"] = tasks
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

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

    def test_dispatch_spawn_done_without_evidence_schedules_retry_claim(self):
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
        self.assertEqual(dispatch["spawn"]["reasonCode"], "incomplete_output", dispatch)
        self.assertEqual((dispatch.get("closeApply") or {}).get("intent"), "claim_task", dispatch)

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
        self.assertEqual(status["task"]["status"], "in_progress", status)
        self.assertEqual(status["task"]["owner"], "invest-analyst", status)

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
        self.assertIn("DONE_GATE_HINTS", prompt, out)
        self.assertIn("pytest", prompt, out)

    def test_dispatch_prompt_keeps_long_objective_without_tail_truncation(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040L: 长任务描述提示词保留测试",
        ])
        long_objective = "A" * 6200 + "TAIL_MARKER_KEEP_ME"
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040L",
            "--agent",
            "coder",
            "--task",
            long_objective,
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"已完成并测试通过","evidence":["logs/t040l.log","pytest passed"]}',
        ])
        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertIn("TAIL_MARKER_KEEP_ME", prompt, out)
        self.assertGreater(len(prompt), 6000, out)

    def test_debugger_prompt_includes_subagent_hint(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@debugger create task T-040D: 复杂排障任务",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040D",
            "--agent",
            "debugger",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"已完成并验证","evidence":["logs/t040d.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertIn("COLLABORATION_HINTS", prompt, out)
        self.assertIn("enable subagent workflow", prompt, out)

    def test_spawn_timeout_zero_disables_subprocess_timeout_and_openclaw_timeout_flag(self):
        module = load_milestone_module()
        real_run = module.subprocess.run
        captured = {"timeout": "unset", "cmd": []}

        class FakeProc:
            returncode = 0
            stdout = '{"status":"done","summary":"日志核验通过","evidence":["logs/ts0.log"]}'
            stderr = ""

        def fake_run(cmd, capture_output, text, check, timeout):
            captured["cmd"] = list(cmd)
            captured["timeout"] = timeout
            return FakeProc()

        args = argparse.Namespace(
            root=str(self.root),
            task_id="T-TS0",
            agent="debugger",
            timeout_sec=0,
            spawn_cmd="",
            mode="send",
            spawn_output="",
        )
        try:
            module.subprocess.run = fake_run
            out = module.run_dispatch_spawn(args, "T-TS0: timeout=0 should be unlimited")
        finally:
            module.subprocess.run = real_run

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("decision"), "done", out)
        self.assertIsNone(captured["timeout"], captured)
        self.assertNotIn("--timeout", captured["cmd"], captured)

    def test_spawn_timeout_exception_returns_blocked_instead_of_crash(self):
        module = load_milestone_module()
        real_run = module.subprocess.run

        def fake_run(cmd, capture_output, text, check, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output='{"status":"progress"}', stderr="model busy")

        args = argparse.Namespace(
            root=str(self.root),
            task_id="T-TS1",
            agent="invest-analyst",
            timeout_sec=120,
            spawn_cmd="",
            mode="send",
            spawn_output="",
        )
        try:
            module.subprocess.run = fake_run
            out = module.run_dispatch_spawn(args, "T-TS1: timeout should not crash dispatch")
        finally:
            module.subprocess.run = real_run

        self.assertFalse(out.get("ok"), out)
        self.assertEqual(out.get("decision"), "blocked", out)
        self.assertEqual(out.get("reasonCode"), "spawn_failed", out)
        self.assertEqual(out.get("spawnErrorKind"), "timeout", out)
        self.assertIn("timeout", str(out.get("error") or "").lower(), out)

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

    def test_dispatch_parses_nested_worker_json_report_from_openclaw_payloads(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@paper-summarizer create task T-042B: 包装输出解析",
        ])
        wrapped = {
            "runId": "demo-run",
            "status": "ok",
            "summary": "completed",
            "result": {
                "payloads": [
                    {
                        "text": "阶段日志: 之前遇到 blocked/error 的历史记录，仅用于调试。",
                        "mediaUrl": None,
                    },
                    {
                        "text": (
                            "```json\n"
                            "{\n"
                            '  "taskId": "T-042B",\n'
                            '  "agent": "paper-summarizer",\n'
                            '  "status": "done",\n'
                            '  "summary": "摘要summary已完成，包含5条要点与结论",\n'
                            '  "changes": [{"path":"artifacts/t042b-summary.md","summary":"write summary bullets"}],\n'
                            '  "evidence": ["artifacts/t042b-summary.md","https://example.com/source-a"],\n'
                            '  "risks": [],\n'
                            '  "nextActions": []\n'
                            "}\n"
                            "```"
                        ),
                        "mediaUrl": None,
                    },
                ]
            },
        }
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-042B",
            "--agent",
            "paper-summarizer",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            json.dumps(wrapped, ensure_ascii=False),
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["spawn"]["decision"], "done", out)
        self.assertEqual(out["spawn"]["reasonCode"], "done_with_evidence", out)
        self.assertEqual((out.get("closeApply") or {}).get("status"), "done", out)
        report = (out.get("spawn") or {}).get("normalizedReport") or {}
        self.assertEqual(report.get("status"), "done", out)
        self.assertIn("artifacts/t042b-summary.md", " ".join(report.get("hardEvidence") or []), out)

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
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual((out.get("closeApply") or {}).get("intent"), "claim_task", out)
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
        self.assertEqual(status["task"]["status"], "in_progress", status)
        self.assertEqual(status["task"]["owner"], "debugger", status)

    def test_user_friendly_help_and_project_status_alias(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-050: 帮助命令测试",
        ])

        help_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 帮助",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(help_out["ok"], help_out)
        self.assertEqual(help_out.get("intent"), "help", help_out)
        self.assertIn("开始项目", help_out["send"]["payload"]["text"], help_out)

        status_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 项目状态",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(status_out["ok"], status_out)
        self.assertEqual(status_out.get("intent"), "status", status_out)

    def test_user_friendly_autopilot_toggle_commands(self):
        opened = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 自动推进 开 2",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened.get("intent"), "auto_progress", opened)
        self.assertTrue((opened.get("state") or {}).get("enabled"), opened)
        self.assertEqual((opened.get("state") or {}).get("maxSteps"), 2, opened)
        self.assertTrue(((opened.get("scheduler") or {}).get("state") or {}).get("enabled"), opened)
        self.assertEqual(((opened.get("scheduler") or {}).get("state") or {}).get("maxSteps"), 2, opened)

        status = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 自动推进 状态",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(status["ok"], status)
        self.assertEqual(status.get("intent"), "auto_progress", status)
        self.assertTrue((status.get("state") or {}).get("enabled"), status)

        closed = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 自动推进 关",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(closed["ok"], closed)
        self.assertEqual(closed.get("intent"), "auto_progress", closed)
        self.assertFalse((closed.get("state") or {}).get("enabled"), closed)
        self.assertFalse(((closed.get("scheduler") or {}).get("state") or {}).get("enabled"), closed)

    def test_auto_progress_send_mode_attempts_scheduler_daemon_bootstrap(self):
        module = load_milestone_module()
        real_send = module.send_group_message
        real_popen = module.subprocess.Popen
        out_buffer = io.StringIO()
        popen_calls = {"count": 0}

        class FakePopen:
            def __init__(self, pid: int):
                self.pid = pid

        def fake_popen(cmd, stdout=None, stderr=None, start_new_session=False):
            popen_calls["count"] += 1
            return FakePopen(43210)

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator 自动推进 开 2",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=120,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode="milestone_only",
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        try:
            module.send_group_message = lambda *unused_args, **unused_kwargs: {"ok": True, "dryRun": True}
            module.subprocess.Popen = fake_popen
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send
            module.subprocess.Popen = real_popen

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertEqual(payload.get("intent"), "auto_progress", payload)
        daemon_bootstrap = payload.get("daemonBootstrap") or {}
        self.assertTrue(daemon_bootstrap.get("attempted"), payload)
        self.assertEqual(daemon_bootstrap.get("status"), "started", payload)
        self.assertEqual(popen_calls["count"], 1, payload)

    def test_control_panel_advance_once_command_hits_explicit_branch(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-051: 推进一次命令应命中分支",
        ])

        out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 推进一次",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("handled"), out)
        self.assertEqual(out.get("intent"), "advance_once", out)
        run_payload = out.get("run") or {}
        self.assertEqual(run_payload.get("intent"), "autopilot", out)
        self.assertEqual(run_payload.get("maxSteps"), 1, out)
        self.assertEqual(run_payload.get("stepsRun"), 1, out)

    def test_user_friendly_start_project_bootstrap(self):
        proj = self.root / "demo-project"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "PRD.md").write_text(
            "\n".join(
                [
                    "# Demo",
                    "## 14. 里程碑建议",
                    "- M1：数据模型 + 指标",
                    "- M2：周频推荐 + 报告输出",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            f"@orchestrator 开始项目 {proj}",
            "--mode",
            "dry-run",
            "--dispatch-spawn",
            "--spawn-output",
            '{"status":"done","summary":"初始化完成，测试通过","evidence":["logs/start.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("intent"), "start_project", out)
        self.assertGreaterEqual(out.get("createdCount", 0), 2, out)
        self.assertGreaterEqual(out.get("decompositionCount", 0), 2, out)
        self.assertIsInstance(out.get("confidenceSummary"), dict, out)
        self.assertTrue((out.get("bootstrap") or {}).get("ok"), out)

        t001 = run_json([
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
        self.assertEqual(t001["task"]["status"], "done", t001)

        created_ids = out.get("createdTaskIds") or []
        if len(created_ids) >= 2:
            second = run_json([
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"status {created_ids[1]}",
            ])
            self.assertIn(created_ids[0], second["task"].get("dependsOn") or [], second)

    def test_xhs_bootstrap_creates_stage_chain_and_prompt_bindings(self):
        workflow_root, pdf_path = self._prepare_xhs_inputs("P-101")
        out = run_json([
            "python3",
            str(MILE),
            "xhs-bootstrap",
            "--root",
            str(self.root),
            "--paper-id",
            "P-101",
            "--pdf-path",
            str(pdf_path),
            "--workflow-root",
            str(workflow_root),
            "--mode",
            "dry-run",
            "--no-spawn",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("intent"), "xhs_bootstrap", out)
        created_ids = out.get("createdTaskIds") or []
        self.assertEqual(len(created_ids), self.XHS_STAGE_COUNT, out)
        self.assertIn("dependsOnSync", out, out)
        self.assertIn("bootstrap", out, out)

        context_state = json.loads((self.root / "state" / "task-context-map.json").read_text(encoding="utf-8"))
        context_tasks = context_state.get("tasks") or {}
        self.assertEqual(len(context_tasks), self.XHS_STAGE_COUNT, context_state)

        for idx, task_id in enumerate(created_ids):
            entry = context_tasks.get(task_id) or {}
            prompt = str(entry.get("dispatchPrompt") or "")
            self.assertEqual(entry.get("projectPath"), str(workflow_root), entry)
            self.assertTrue(prompt.strip(), (task_id, entry))
            self.assertIn("P-101", prompt, (task_id, prompt))
            self.assertIn(str(pdf_path), prompt, (task_id, prompt))
            self.assertIn('"taskId"', prompt, (task_id, prompt))

            status = run_json([
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"status {task_id}",
            ])
            expected_depends = [created_ids[idx - 1]] if idx > 0 else []
            self.assertEqual(status["task"].get("dependsOn") or [], expected_depends, status)

    def test_dispatch_run_autopilot_use_bound_dispatch_prompt_when_task_is_implicit(self):
        for task_id in ("T-201", "T-202", "T-203"):
            run_json([
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"@coder create task {task_id}: dispatchPrompt fallback",
            ])

        self._bind_task_context("T-201", "BOUND_PROMPT_DISPATCH")
        self._bind_task_context("T-202", "BOUND_PROMPT_RUN")
        self._bind_task_context("T-203", "BOUND_PROMPT_AUTOPILOT")

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-201",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"done","evidence":["logs/dispatch.log"]}',
        ])
        self.assertTrue(dispatch["ok"], dispatch)
        self.assertIn("BOUND_PROMPT_DISPATCH", dispatch.get("agentPrompt", ""), dispatch)

        run_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator run T-202",
            "--mode",
            "dry-run",
            "--dispatch-spawn",
            "--spawn-output",
            '{"status":"done","summary":"done","evidence":["logs/run.log"]}',
        ])
        self.assertTrue(run_out["ok"], run_out)
        self.assertIn("BOUND_PROMPT_RUN", run_out.get("agentPrompt", ""), run_out)

        autopilot = run_json([
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
            '{"status":"done","summary":"done","evidence":["logs/auto.log"]}',
        ])
        self.assertTrue(autopilot["ok"], autopilot)
        steps = autopilot.get("steps") or []
        self.assertEqual(len(steps), 1, autopilot)
        prompt = ((steps[0].get("dispatch") or {}).get("agentPrompt") or "")
        self.assertIn("BOUND_PROMPT_AUTOPILOT", prompt, autopilot)

    def test_feishu_router_supports_xhs_bootstrap_intents(self):
        workflow_root, pdf_path = self._prepare_xhs_inputs("P-201")
        for command in (
            f"@orchestrator 开始xhs流程 P-201 {pdf_path}",
            f"@orchestrator start xhs workflow P-202 {pdf_path}",
        ):
            out = run_json([
                "python3",
                str(MILE),
                "feishu-router",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                command,
                "--mode",
                "dry-run",
                "--dispatch-manual",
            ])
            self.assertTrue(out["ok"], out)
            self.assertEqual(out.get("intent"), "xhs_bootstrap", out)
            self.assertEqual(len(out.get("createdTaskIds") or []), self.XHS_STAGE_COUNT, out)
            self.assertIn("dependsOnSync", out, out)
            self.assertIn("bootstrap", out, out)

    def test_feishu_router_supports_xhs_n8n_trigger_intents(self):
        _, pdf_path = self._prepare_xhs_inputs("P-301")
        for command in (
            f"@orchestrator 开始xhs流程n8n P-301 {pdf_path}",
            f"@orchestrator 开始xhs流程 n8n P-302 {pdf_path}",
            f"@orchestrator start xhs n8n P-303 {pdf_path}",
        ):
            out = run_json([
                "python3",
                str(MILE),
                "feishu-router",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                command,
                "--mode",
                "dry-run",
            ])
            self.assertTrue(out["ok"], out)
            self.assertEqual(out.get("intent"), "xhs_n8n_trigger", out)
            self.assertTrue(out.get("dryRun"), out)
            planned = out.get("plannedCommand") or []
            self.assertTrue(any("trigger-xhs-workflow.sh" in str(x) for x in planned), out)

    def test_scheme_b_coder_dispatch_uses_claude_worker_executor(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-060: codex worker 路由测试",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-060",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/one-step.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("executor"), "claude_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("claude_worker_bridge.py" in str(x) for x in planned), out)
        metrics = spawn.get("metrics") or {}
        self.assertIn("tokenUsage", metrics, out)

    def test_scheme_b_debugger_dispatch_uses_codex_worker_executor(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@debugger create task T-061: 非 coder 路由保持",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-061",
            "--agent",
            "debugger",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/route.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("executor"), "codex_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("codex_worker_bridge.py" in str(x) for x in planned), out)

    def test_writing_task_forces_claude_executor_even_for_debugger(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@debugger create task T-061W: Stage B Draft XHS summary writing",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-061W",
            "--agent",
            "debugger",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/route.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("executor"), "claude_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("claude_worker_bridge.py" in str(x) for x in planned), out)

    def test_scheme_b_other_roles_keep_openclaw_executor(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@invest-analyst create task T-062: 其他角色走 openclaw",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-062",
            "--agent",
            "invest-analyst",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/route.log"]}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("executor"), "openclaw_agent", out)

    def test_scheme_b_executor_routing_can_be_overridden_by_runtime_policy(self):
        runtime_policy_path = self.root / "config" / "runtime-policy.json"
        runtime_policy_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_policy_path.write_text(
            json.dumps(
                {
                    "orchestrator": {
                        "executorRouting": {
                            "coder": "openclaw_agent",
                            "debugger": "openclaw_agent",
                            "invest-analyst": "codex_cli",
                        }
                    }
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-063: runtime policy override coder",
        ])
        coder_out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-063",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/route.log"]}',
        ])
        self.assertTrue(coder_out["ok"], coder_out)
        self.assertEqual((coder_out.get("spawn") or {}).get("executor"), "openclaw_agent", coder_out)

        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@invest-analyst create task T-064: runtime policy override analyst",
        ])
        analyst_out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--task-id",
            "T-064",
            "--agent",
            "invest-analyst",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"完成","evidence":["logs/route.log"]}',
        ])
        self.assertTrue(analyst_out["ok"], analyst_out)
        spawn = analyst_out.get("spawn") or {}
        self.assertEqual(spawn.get("executor"), "codex_cli", analyst_out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("codex_worker_bridge.py" in str(x) for x in planned), analyst_out)

    def test_scheduler_run_updates_state_and_executes_autopilot(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-070: scheduler 内核测试",
        ])

        first = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--action",
            "enable",
            "--interval-sec",
            "60",
            "--max-steps",
            "1",
            "--spawn",
            "--mode",
            "dry-run",
            "--spawn-output",
            '{"status":"done","summary":"scheduler-done","evidence":["logs/scheduler.log"]}',
        ])
        self.assertTrue(first["ok"], first)
        self.assertEqual(first.get("intent"), "scheduler_run", first)
        self.assertTrue((first.get("state") or {}).get("enabled"), first)
        self.assertFalse(first.get("skipped"), first)
        self.assertEqual((first.get("run") or {}).get("stepsRun"), 1, first)

        second = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--mode",
            "dry-run",
        ])
        self.assertTrue(second["ok"], second)
        self.assertTrue(second.get("skipped"), second)
        self.assertEqual(second.get("reason"), "not_due", second)

    def test_scheduler_run_skipped_does_not_advance_timestamps(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-071: scheduler skip 不推进时间戳",
        ])

        enabled = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--action",
            "enable",
            "--interval-sec",
            "60",
            "--max-steps",
            "1",
            "--spawn",
            "--mode",
            "dry-run",
            "--spawn-output",
            '{"status":"done","summary":"scheduler-enable","evidence":["logs/enable.log"]}',
        ])
        self.assertTrue(enabled.get("ok"), enabled)
        before = enabled.get("state") or {}
        before_last_run = int(before.get("lastRunTs") or 0)
        before_next_due = int(before.get("nextDueTs") or 0)
        self.assertGreater(before_last_run, 0, enabled)
        self.assertGreater(before_next_due, before_last_run, enabled)

        frozen = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 治理 冻结",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(frozen.get("ok"), frozen)

        skipped = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--action",
            "tick",
            "--force",
            "--spawn",
            "--mode",
            "dry-run",
            "--spawn-output",
            '{"status":"done","summary":"scheduler-force","evidence":["logs/force.log"]}',
        ])
        self.assertTrue(skipped.get("ok"), skipped)
        self.assertTrue(skipped.get("skipped"), skipped)
        self.assertEqual(skipped.get("reason"), "governance_frozen", skipped)
        run = skipped.get("run") or {}
        self.assertTrue(run.get("skipped"), skipped)
        self.assertEqual(run.get("reason"), "governance_frozen", skipped)
        after = skipped.get("state") or {}
        self.assertEqual(int(after.get("lastRunTs") or 0), before_last_run, skipped)
        self.assertEqual(int(after.get("nextDueTs") or 0), before_next_due, skipped)

    def test_feishu_router_scheduler_control_commands(self):
        enabled = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 调度 开 1",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(enabled["ok"], enabled)
        self.assertEqual(enabled.get("intent"), "scheduler_control", enabled)
        self.assertTrue((enabled.get("state") or {}).get("enabled"), enabled)
        self.assertEqual((enabled.get("state") or {}).get("intervalSec"), 60, enabled)

        status = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 调度 状态",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(status["ok"], status)
        self.assertEqual(status.get("intent"), "scheduler_control", status)
        self.assertTrue((status.get("state") or {}).get("enabled"), status)

        disabled = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 调度 关",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(disabled["ok"], disabled)
        self.assertEqual(disabled.get("intent"), "scheduler_control", disabled)
        self.assertFalse((disabled.get("state") or {}).get("enabled"), disabled)

    def test_scheduler_daemon_runs_multiple_loops(self):
        enabled = run_json([
            "python3",
            str(MILE),
            "scheduler-run",
            "--root",
            str(self.root),
            "--action",
            "enable",
            "--interval-sec",
            "60",
            "--mode",
            "dry-run",
            "--no-spawn",
        ])
        self.assertTrue(enabled["ok"], enabled)
        self.assertTrue((enabled.get("state") or {}).get("enabled"), enabled)

        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-072: daemon loop one",
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
            "@coder create task T-073: daemon loop two",
        ])

        out = run_json([
            "python3",
            str(MILE),
            "scheduler-daemon",
            "--root",
            str(self.root),
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"daemon-done","evidence":["logs/daemon.log"]}',
            "--force",
            "--poll-sec",
            "0",
            "--max-loops",
            "2",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("intent"), "scheduler_daemon", out)
        self.assertEqual(out.get("loops"), 2, out)
        self.assertEqual(out.get("runs"), 2, out)

        t72 = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-072",
        ])
        t73 = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-073",
        ])
        self.assertEqual((t72.get("task") or {}).get("status"), "done", t72)
        self.assertEqual((t73.get("task") or {}).get("status"), "done", t73)

    def test_feishu_router_control_panel_card(self):
        out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator 控制台",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("intent"), "control_panel", out)
        send = out.get("send") or {}
        card = ((send.get("payload") or {}).get("card") or {})
        self.assertNotIn("text", (send.get("payload") or {}), out)
        actions = card.get("actions") if isinstance(card, dict) else []
        self.assertTrue(isinstance(actions, list) and actions, out)
        titles = [str((a or {}).get("title") or "") for a in actions]
        self.assertTrue(any("开始项目" in t for t in titles), out)
        self.assertTrue(any("推进一次" in t for t in titles), out)
        self.assertTrue(any("查看阻塞" in t for t in titles), out)
        self.assertTrue(any("验收摘要" in t for t in titles), out)

    def test_status_full_includes_ops_metrics(self):
        now_ts = int(time.time())
        metrics_path = self.root / "state" / "ops.metrics.jsonl"
        metrics_path.write_text(
            json.dumps(
                {
                    "event": "dispatch_done",
                    "at": "2026-03-01T00:00:00Z",
                    "ts": now_ts,
                    "taskId": "T-OPS-1",
                    "cycleMs": 1200,
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )

        out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator status full",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        self.assertTrue(out.get("full"), out)
        self.assertIn("opsMetrics", out, out)
        self.assertEqual((out.get("opsMetrics") or {}).get("windowDays"), 7, out)
        self.assertEqual((out.get("opsMetrics") or {}).get("throughputCompleted"), 1, out)

    def test_status_full_surfaces_ops_metrics_error(self):
        module = load_milestone_module()
        real_aggregate = module.ops_metrics.aggregate_metrics
        out_buffer = io.StringIO()

        def boom(*_args, **_kwargs):
            raise RuntimeError("ops metrics exploded")

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator status full",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="dry-run",
            session_id="",
            timeout_sec=120,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode="milestone_only",
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        try:
            module.ops_metrics.aggregate_metrics = boom
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.ops_metrics.aggregate_metrics = real_aggregate

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertEqual(payload.get("intent"), "status", payload)
        self.assertTrue(payload.get("full"), payload)
        self.assertEqual(payload.get("opsMetrics"), {}, payload)
        self.assertIn("opsMetricsError", payload)
        self.assertIn("ops metrics exploded", str(payload.get("opsMetricsError")), payload)

    def test_send_group_card_prefers_direct_feishu_api_before_text_fallback(self):
        module = load_milestone_module()
        real_run = module.subprocess.run
        text_calls = {"count": 0}

        class FakeProc:
            def __init__(self, returncode: int, stdout: str, stderr: str = ""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, capture_output, text, check, timeout):
            if "--card" in cmd:
                return FakeProc(0, '{"payload":{"result":{}}}')
            if "--message" in cmd:
                text_calls["count"] += 1
                return FakeProc(0, '{"payload":{"result":{"messageId":"om_text_fallback"}}}')
            return FakeProc(1, "", "unexpected command")

        try:
            module.subprocess.run = fake_run
            module.send_group_card_via_feishu_api = lambda *args, **kwargs: {
                "ok": True,
                "messageId": "om_direct_card",
            }
            out = module.send_group_card(
                "oc_test_group",
                "orchestrator",
                {"schema": "2.0", "body": {"elements": []}},
                "send",
                fallback_text="[TASK] fallback text",
            )
        finally:
            module.subprocess.run = real_run

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("recoveredBy"), "direct_feishu_api", out)
        self.assertEqual(text_calls["count"], 0, out)

    def test_send_group_card_via_feishu_api_treats_code_zero_as_success(self):
        module = load_milestone_module()
        real_load = module.load_openclaw_feishu_credentials
        real_post = module.feishu_post_json
        calls = {"count": 0}

        def fake_post_json(url, payload, headers=None, timeout_sec=20):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"code": 0, "msg": "ok", "tenant_access_token": "tok"}
            return {"code": 0, "msg": "success", "data": {"message_id": "om_card_ok"}}

        try:
            module.load_openclaw_feishu_credentials = lambda account_id: {
                "appId": "app_x",
                "appSecret": "sec_x",
                "host": "open.feishu.cn",
            }
            module.feishu_post_json = fake_post_json
            out = module.send_group_card_via_feishu_api(
                "oc_test_group",
                "orchestrator",
                {"schema": "2.0", "body": {"elements": []}},
            )
        finally:
            module.load_openclaw_feishu_credentials = real_load
            module.feishu_post_json = real_post

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("messageId"), "om_card_ok", out)

    def test_send_group_card_via_feishu_api_converts_adaptive_card_to_feishu_card_format(self):
        module = load_milestone_module()
        real_load = module.load_openclaw_feishu_credentials
        real_post = module.feishu_post_json
        sent_payload = {"content": None}
        calls = {"count": 0}

        def fake_post_json(url, payload, headers=None, timeout_sec=20):
            calls["count"] += 1
            if calls["count"] == 1:
                return {"code": 0, "msg": "ok", "tenant_access_token": "tok"}
            sent_payload["content"] = payload.get("content")
            return {"code": 0, "msg": "success", "data": {"message_id": "om_card_ok"}}

        try:
            module.load_openclaw_feishu_credentials = lambda account_id: {
                "appId": "app_x",
                "appSecret": "sec_x",
                "host": "open.feishu.cn",
            }
            module.feishu_post_json = fake_post_json
            out = module.send_group_card_via_feishu_api(
                "oc_test_group",
                "orchestrator",
                {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": "Orchestrator 控制台"},
                    ],
                    "actions": [
                        {
                            "type": "Action.Submit",
                            "title": "推进一次",
                            "data": {"command": "@orchestrator 推进一次"},
                        }
                    ],
                },
            )
        finally:
            module.load_openclaw_feishu_credentials = real_load
            module.feishu_post_json = real_post

        self.assertTrue(out.get("ok"), out)
        content = json.loads(sent_payload["content"] or "{}")
        self.assertNotEqual(content.get("type"), "AdaptiveCard", content)
        self.assertTrue(isinstance(content.get("elements"), list), content)


if __name__ == "__main__":
    unittest.main()
