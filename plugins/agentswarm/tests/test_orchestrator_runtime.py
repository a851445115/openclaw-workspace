import argparse
import contextlib
import json
import io
import multiprocessing
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock
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
INTERVENE = SCRIPTS / "intervene-task"


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


def _continuation_same_key_worker(root: str, rounds: int, start_event, result_queue) -> None:
    try:
        module = load_milestone_module()
        if not start_event.wait(timeout=10):
            raise TimeoutError("start_event timed out")
        for round_index in range(rounds):
            checkpoint = {
                "progressPercent": 50,
                "completed": ["triage"],
                "remaining": ["patch"],
                "nextAction": "continue",
                "continueHint": "continue",
                "stallSignal": "none",
                "evidenceDelta": [f"worker-{os.getpid()}-{round_index}"],
            }
            out = module.evaluate_checkpoint_continuation(
                root,
                "T-CP-LOCK",
                "continuation lock test",
                {"status": "progress", "checkpoint": checkpoint},
                persist_state=True,
                now_ts=1_700_300_000 + round_index,
            )
            if str((out or {}).get("decision") or "") != "continue":
                raise AssertionError(f"unexpected continuation decision: {out}")
        result_queue.put({"ok": True})
    except Exception as err:
        result_queue.put({"ok": False, "error": repr(err)})


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

    def _read_collab_messages(self):
        path = self.root / "state" / "collab.messages.jsonl"
        if not path.exists():
            return []
        rows = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows

    def _write_json_file(self, rel_path: str, payload):
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _scheduler_args(self, *, action: str = "tick", force: bool = True):
        return argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            action=action,
            interval_sec=None,
            max_steps=None,
            force=force,
            group_id="oc_test",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=0,
            spawn=False,
            spawn_cmd="",
            spawn_output="",
            visibility_mode="handoff_visible",
            session_id="",
        )

    def _read_task_snapshot(self):
        path = self.root / "state" / "tasks.snapshot.json"
        return json.loads(path.read_text(encoding="utf-8") or "{}")

    def _read_interventions_state(self):
        path = self.root / "state" / "interventions.json"
        if not path.exists():
            return {"tasks": {}, "updatedAt": ""}
        return json.loads(path.read_text(encoding="utf-8") or "{}")

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

    def test_dispatch_blocked_high_risk_reason_triggers_expert_group(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-006: 高风险阻塞触发专家组",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-006",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        self.assertEqual(dispatch["spawn"]["decision"], "blocked", dispatch)
        self.assertEqual(dispatch["spawn"]["reasonCode"], "spawn_failed", dispatch)

        expert_group = dispatch.get("expertGroup") or {}
        self.assertTrue(expert_group.get("triggered"), dispatch)
        self.assertIn("high_risk_reason", expert_group.get("reasons") or [], dispatch)
        self.assertGreaterEqual(int(expert_group.get("score") or 0), 1, dispatch)
        templates = expert_group.get("templates") or []
        self.assertIsInstance(templates, list, dispatch)
        self.assertGreaterEqual(len(templates), 3, dispatch)
        role_map = {str(item.get("role") or "") for item in templates if isinstance(item, dict)}
        self.assertTrue({"coder", "debugger", "invest-analyst"}.issubset(role_map), dispatch)
        for item in templates:
            self.assertIsInstance(item.get("requiredFields"), list, dispatch)
            self.assertIn("hypothesis", item.get("requiredFields") or [], dispatch)
            self.assertIn("evidence", item.get("requiredFields") or [], dispatch)
            self.assertIn("confidence", item.get("requiredFields") or [], dispatch)
            self.assertIn("proposedFix", item.get("requiredFields") or [], dispatch)
            self.assertIn("risk", item.get("requiredFields") or [], dispatch)
        consensus = expert_group.get("consensus") or {}
        self.assertIn("consensusPlan", consensus, dispatch)
        self.assertIn("owner", consensus, dispatch)
        self.assertIn("executionChecklist", consensus, dispatch)
        self.assertIn("acceptanceGate", consensus, dispatch)

    def test_dispatch_blocked_non_high_risk_under_threshold_not_triggered(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-007: 非高危阻塞不触发专家组",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-007",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting for upstream data"}',
        ])
        self.assertEqual(dispatch["spawn"]["decision"], "blocked", dispatch)
        self.assertEqual(dispatch["spawn"]["reasonCode"], "blocked_signal", dispatch)

        expert_group = dispatch.get("expertGroup") or {}
        self.assertFalse(expert_group.get("triggered"), dispatch)
        self.assertEqual(expert_group.get("score"), 0, dispatch)
        self.assertEqual(expert_group.get("reasons"), [], dispatch)
        consensus = expert_group.get("consensus") or {}
        self.assertEqual(consensus.get("consensusPlan"), "", dispatch)
        self.assertEqual(consensus.get("executionChecklist"), [], dispatch)
        self.assertEqual(consensus.get("acceptanceGate"), [], dispatch)

    def test_dispatch_blocked_retry_limit_triggers_expert_group_with_non_high_risk_reason(self):
        self._write_json_file(
            "config/recovery-policy.json",
            {
                "default": {"maxAttempts": 5, "cooldownSec": 0},
                "reasonPolicies": {
                    "blocked_signal": {"maxAttempts": 5, "cooldownSec": 0},
                },
            },
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
            "@coder create task T-007A: retry_limit 非高危触发",
        ])

        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-007A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting for upstream data"}',
        ])
        self.assertEqual(first["spawn"]["reasonCode"], "blocked_signal", first)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-007A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting for upstream data"}',
        ])
        self.assertEqual(second["spawn"]["reasonCode"], "blocked_signal", second)
        expert_group = second.get("expertGroup") or {}
        self.assertTrue(expert_group.get("triggered"), second)
        self.assertIn("retry_limit", expert_group.get("reasons") or [], second)
        self.assertNotIn("high_risk_reason", expert_group.get("reasons") or [], second)

    def test_dispatch_blocked_duration_triggers_expert_group_from_latest_snapshot(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@invest-analyst create task T-007B: blocked_duration 触发",
        ])
        module = load_milestone_module()
        real_time = module.time.time
        future_ts = time.time() + 3600
        args = argparse.Namespace(
            root=self.root.as_posix(),
            task_id="T-007B",
            agent="invest-analyst",
            task="T-007B: blocked_duration 触发",
            actor="orchestrator",
            session_id="",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=120,
            spawn=True,
            spawn_cmd="",
            spawn_output='{"message":"[BLOCKED] waiting for upstream data"}',
            visibility_mode="handoff_visible",
        )
        try:
            module.time.time = lambda: future_ts
            out = module.dispatch_once(args)
        finally:
            module.time.time = real_time

        self.assertTrue(out["ok"], out)
        self.assertEqual((out.get("closeApply") or {}).get("intent"), "block_task", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "blocked_signal", out)
        expert_group = out.get("expertGroup") or {}
        self.assertTrue(expert_group.get("triggered"), out)
        self.assertIn("blocked_duration", expert_group.get("reasons") or [], out)

    def test_dispatch_blocked_downstream_impact_triggers_expert_group(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-007C: downstream_impact 上游任务",
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
            "@coder create task T-107: downstream-1",
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
            "@coder create task T-108: downstream-2",
        ])
        snapshot_path = self.root / "state" / "tasks.snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["tasks"]["T-107"]["dependsOn"] = ["T-007C"]
        snapshot["tasks"]["T-108"]["blockedBy"] = ["T-007C"]
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-007C",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting for upstream data"}',
        ])
        self.assertEqual(dispatch["spawn"]["reasonCode"], "blocked_signal", dispatch)
        expert_group = dispatch.get("expertGroup") or {}
        self.assertTrue(expert_group.get("triggered"), dispatch)
        self.assertIn("downstream_impact", expert_group.get("reasons") or [], dispatch)

    def test_dispatch_expert_group_disabled_keeps_blocked_result_untriggered(self):
        self._write_json_file("config/expert-group-policy.json", {"enabled": False})
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-007D: expert group disabled",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-007D",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        self.assertEqual(dispatch["spawn"]["reasonCode"], "spawn_failed", dispatch)
        expert_group = dispatch.get("expertGroup") or {}
        self.assertFalse(expert_group.get("triggered"), dispatch)
        self.assertEqual(expert_group.get("reasons"), [], dispatch)

    def test_dispatch_expert_group_output_shape_is_stable(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-008: 专家组输出结构稳定性",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting for external API quota"}',
        ])
        expert_group = dispatch.get("expertGroup")
        self.assertIsInstance(expert_group, dict, dispatch)
        self.assertIn("triggered", expert_group, dispatch)
        self.assertIn("reasons", expert_group, dispatch)
        self.assertIn("score", expert_group, dispatch)
        self.assertIn("policyDigest", expert_group, dispatch)
        self.assertIn("templates", expert_group, dispatch)
        self.assertIn("consensus", expert_group, dispatch)
        self.assertIn("lifecycle", expert_group, dispatch)
        self.assertIsInstance(expert_group.get("reasons"), list, dispatch)
        self.assertIsInstance(expert_group.get("score"), int, dispatch)
        self.assertIsInstance(expert_group.get("templates"), list, dispatch)
        consensus = expert_group.get("consensus") or {}
        self.assertIsInstance(consensus, dict, dispatch)
        self.assertIn("consensusPlan", consensus, dispatch)
        self.assertIn("owner", consensus, dispatch)
        self.assertIn("executionChecklist", consensus, dispatch)
        self.assertIn("acceptanceGate", consensus, dispatch)
        lifecycle = expert_group.get("lifecycle") or {}
        self.assertIsInstance(lifecycle, dict, dispatch)
        self.assertIn("groupId", lifecycle, dispatch)
        self.assertIn("status", lifecycle, dispatch)
        self.assertIn("path", lifecycle, dispatch)
        self.assertIn("historyCount", lifecycle, dispatch)

    def test_dispatch_expert_group_lifecycle_persists_and_progresses(self):
        self._write_json_file(
            "config/recovery-policy.json",
            {
                "default": {"maxAttempts": 6, "cooldownSec": 0},
                "reasonPolicies": {
                    "spawn_failed": {"maxAttempts": 6, "cooldownSec": 0},
                    "blocked_signal": {"maxAttempts": 6, "cooldownSec": 0},
                },
            },
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
            "@coder create task T-008A: 生命周期管理",
        ])

        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        lifecycle1 = ((first.get("expertGroup") or {}).get("lifecycle") or {})
        group_id = str(lifecycle1.get("groupId") or "")
        self.assertTrue(group_id, first)
        self.assertEqual(lifecycle1.get("status"), "created", first)
        lifecycle_path = self.root / "state" / "expert-groups" / f"{group_id}.json"
        self.assertTrue(lifecycle_path.exists(), first)
        record1 = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        self.assertEqual(record1.get("status"), "created", record1)
        self.assertEqual(len(record1.get("history") or []), 1, record1)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        lifecycle2 = ((second.get("expertGroup") or {}).get("lifecycle") or {})
        self.assertEqual(lifecycle2.get("groupId"), group_id, second)
        self.assertEqual(lifecycle2.get("status"), "executing", second)
        self.assertGreaterEqual(int(lifecycle2.get("historyCount") or 0), 2, second)
        record2 = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        self.assertEqual(record2.get("status"), "executing", record2)
        self.assertEqual(len(record2.get("history") or []), 2, record2)

        third = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed","expertOutputs":[{"role":"debugger","confidence":0.92,"hypothesis":"race condition","evidence":"trace id=42","proposedFix":"serialize cache writes","risk":"latency"}]}',
        ])
        lifecycle3 = ((third.get("expertGroup") or {}).get("lifecycle") or {})
        self.assertEqual(lifecycle3.get("groupId"), group_id, third)
        self.assertEqual(lifecycle3.get("status"), "converged", third)
        self.assertGreaterEqual(int(lifecycle3.get("historyCount") or 0), 3, third)
        record3 = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        self.assertEqual(record3.get("status"), "converged", record3)
        self.assertEqual(len(record3.get("history") or []), 3, record3)

        fourth = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008A",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","message":"T-008A 完成，证据: logs/run.log"}',
        ])
        self.assertEqual((fourth.get("spawn") or {}).get("decision"), "done", fourth)
        lifecycle4 = ((fourth.get("expertGroup") or {}).get("lifecycle") or {})
        self.assertEqual(lifecycle4.get("groupId"), group_id, fourth)
        self.assertEqual(lifecycle4.get("status"), "archived", fourth)
        self.assertGreaterEqual(int(lifecycle4.get("historyCount") or 0), 4, fourth)
        record4 = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        self.assertEqual(record4.get("status"), "archived", record4)
        self.assertEqual(len(record4.get("history") or []), 4, record4)

    def test_dispatch_expert_group_weak_outputs_do_not_mark_converged(self):
        self._write_json_file(
            "config/recovery-policy.json",
            {
                "default": {"maxAttempts": 6, "cooldownSec": 0},
                "reasonPolicies": {
                    "spawn_failed": {"maxAttempts": 6, "cooldownSec": 0},
                },
            },
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
            "@coder create task T-008W: 弱输出不收敛",
        ])

        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008W",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        lifecycle1 = ((first.get("expertGroup") or {}).get("lifecycle") or {})
        self.assertEqual(lifecycle1.get("status"), "created", first)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-008W",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed","expertOutputs":[{"role":"debugger","confidence":0.99}]}',
        ])
        lifecycle2 = ((second.get("expertGroup") or {}).get("lifecycle") or {})
        self.assertIn(lifecycle2.get("status"), {"created", "executing"}, second)
        self.assertNotEqual(lifecycle2.get("status"), "converged", second)

    def test_feishu_router_done_wakeup_archives_expert_group_lifecycle(self):
        self._write_json_file(
            "config/recovery-policy.json",
            {
                "default": {"maxAttempts": 6, "cooldownSec": 0},
                "reasonPolicies": {
                    "spawn_failed": {"maxAttempts": 6, "cooldownSec": 0},
                },
            },
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
            "@coder create task T-242: done wakeup archive lifecycle",
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
            "@coder claim task T-242",
        ])

        blocked = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-242",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"failed","message":"worker runtime crashed"}',
        ])
        lifecycle = ((blocked.get("expertGroup") or {}).get("lifecycle") or {})
        group_id = str(lifecycle.get("groupId") or "")
        self.assertTrue(group_id, blocked)
        lifecycle_path = self.root / "state" / "expert-groups" / f"{group_id}.json"
        self.assertTrue(lifecycle_path.exists(), blocked)

        done = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator T-242 已完成，测试通过，证据: logs/t242.log",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(done["ok"], done)
        done_cleanup = done.get("doneCleanup") if isinstance(done.get("doneCleanup"), dict) else {}
        lifecycle_cleanup = done_cleanup.get("lifecycle") if isinstance(done_cleanup.get("lifecycle"), dict) else {}
        self.assertEqual(lifecycle_cleanup.get("status"), "archived", done)

        archived_record = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        self.assertEqual(archived_record.get("status"), "archived", archived_record)

    def test_evaluate_dispatch_expert_group_logs_warning_when_template_build_fails(self):
        module = load_milestone_module()
        original_build_templates = module.expert_group.build_expert_templates
        try:
            def boom(*_args, **_kwargs):
                raise RuntimeError("forced template failure")

            module.expert_group.build_expert_templates = boom
            with self.assertLogs(module.LOGGER.name, level="WARNING") as captured:
                out = module.evaluate_dispatch_expert_group(
                    root=self.root.as_posix(),
                    task_id="T-008B",
                    task={"taskId": "T-008B", "status": "blocked"},
                    spawn={"decision": "blocked", "reasonCode": "spawn_failed"},
                    session_meta={},
                    policy=module.expert_group.DEFAULT_EXPERT_GROUP_POLICY,
                )
        finally:
            module.expert_group.build_expert_templates = original_build_templates

        self.assertTrue(out.get("triggered"), out)
        self.assertEqual(out.get("templates"), [], out)
        self.assertTrue(
            any("expert-group template build failed" in msg for msg in captured.output),
            captured.output,
        )

    def test_evaluate_dispatch_expert_group_logs_warning_when_consensus_build_fails(self):
        module = load_milestone_module()
        original_converge = module.expert_group.converge_expert_conclusions
        try:
            def boom(*_args, **_kwargs):
                raise RuntimeError("forced consensus failure")

            module.expert_group.converge_expert_conclusions = boom
            with self.assertLogs(module.LOGGER.name, level="WARNING") as captured:
                out = module.evaluate_dispatch_expert_group(
                    root=self.root.as_posix(),
                    task_id="T-008C",
                    task={"taskId": "T-008C", "status": "blocked", "owner": "coder"},
                    spawn={"decision": "blocked", "reasonCode": "spawn_failed", "nextAssignee": "debugger"},
                    session_meta={},
                    policy=module.expert_group.DEFAULT_EXPERT_GROUP_POLICY,
                )
        finally:
            module.expert_group.converge_expert_conclusions = original_converge

        self.assertTrue(out.get("triggered"), out)
        self.assertIsInstance(out.get("consensus"), dict, out)
        self.assertTrue(
            any("expert-group consensus build failed" in msg for msg in captured.output),
            captured.output,
        )

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

    def test_feishu_router_done_wakeup_cleans_retry_context_and_session_state(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-202: done wakeup clean",
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
            "@coder create task T-203: should stay active",
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
            "@coder claim task T-202",
        ])

        module = load_milestone_module()
        module.context_pack.record_failure(
            self.root.as_posix(),
            task_id="T-202",
            agent="coder",
            executor="codex_cli",
            prompt_text="first prompt",
            output_text="first blocked",
            blocked_reason="spawn_failed",
        )
        module.context_pack.record_failure(
            self.root.as_posix(),
            task_id="T-203",
            agent="coder",
            executor="codex_cli",
            prompt_text="keep prompt",
            output_text="keep blocked",
            blocked_reason="spawn_failed",
        )
        now_ts = int(time.time())
        module.recovery_loop.decide_recovery(self.root.as_posix(), "T-202", "coder", "spawn_failed", now_ts=now_ts)
        module.recovery_loop.decide_recovery(self.root.as_posix(), "T-203", "coder", "spawn_failed", now_ts=now_ts)
        module.session_registry.ensure_session(self.root.as_posix(), "T-202", "coder", "codex_cli")
        module.session_registry.ensure_session(self.root.as_posix(), "T-203", "coder", "codex_cli")

        done = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator T-202 已完成，测试通过，证据: logs/t202.log",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(done["ok"], done)
        self.assertEqual(done.get("intent"), "wakeup", done)

        self.assertEqual(module.context_pack.build_retry_context(self.root.as_posix(), "T-202"), {})
        self.assertTrue(module.context_pack.build_retry_context(self.root.as_posix(), "T-203"))

        self.assertEqual(module.recovery_loop.get_active_cooldown(self.root.as_posix(), "T-202", now_ts=now_ts), {})
        self.assertTrue(module.recovery_loop.get_active_cooldown(self.root.as_posix(), "T-203", now_ts=now_ts))

        state = module.session_registry.load_registry(self.root.as_posix())
        sessions = state.get("sessions") or {}
        key_done = module.session_registry.session_key("T-202", "coder", "codex_cli")
        key_keep = module.session_registry.session_key("T-203", "coder", "codex_cli")
        self.assertEqual((sessions.get(key_done) or {}).get("status"), "done", sessions)
        self.assertEqual((sessions.get(key_keep) or {}).get("status"), "active", sessions)

    def test_feishu_router_done_wakeup_dry_run_skips_collaboration_relay_persistence(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-220: done relay",
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
            "@coder claim task T-220",
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
            "@orchestrator T-220 已完成，证据: logs/t220.log",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        relay = out.get("collabRelay") if isinstance(out.get("collabRelay"), dict) else {}
        self.assertTrue(relay.get("ok"), out)
        self.assertTrue(relay.get("skipped"), out)
        self.assertEqual(relay.get("reason"), "mode_not_send", out)
        self.assertEqual(relay.get("threadId"), "T-220:coder", out)

        collab_messages = self.root / "state" / "collab.messages.jsonl"
        self.assertFalse(collab_messages.exists(), out)

    def test_feishu_router_progress_wakeup_dry_run_skips_collaboration_relay_persistence(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-221: progress relay",
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
            "@orchestrator T-221 进展同步：主因已定位，正在补充验证数据",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        relay = out.get("collabRelay") if isinstance(out.get("collabRelay"), dict) else {}
        self.assertTrue(relay.get("ok"), out)
        self.assertTrue(relay.get("skipped"), out)
        self.assertEqual(relay.get("reason"), "mode_not_send", out)
        self.assertEqual(relay.get("threadId"), "T-221:coder", out)

        collab_messages = self.root / "state" / "collab.messages.jsonl"
        self.assertFalse(collab_messages.exists(), out)

    def test_feishu_router_done_wakeup_send_relay_persists_decision_payload(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-223: done relay success",
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
            "@coder claim task T-223",
        ])

        module = load_milestone_module()
        real_send_group_message = module.send_group_message
        real_append_message = module.collaboration_hub.append_message
        observed = {}
        out_buffer = io.StringIO()
        args = argparse.Namespace(
            root=str(self.root),
            actor="coder",
            text="@orchestrator T-223 已完成，测试通过，证据: logs/t223.log",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode=module.DEFAULT_VISIBILITY_MODE,
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        def append_with_status(root, payload, *extra_args, **extra_kwargs):
            task = module.get_task(root, "T-223") or {}
            observed["statusBeforeRelay"] = str(task.get("status") or "")
            return real_append_message(root, payload, *extra_args, **extra_kwargs)

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True, "dryRun": False}
            module.collaboration_hub.append_message = append_with_status
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send_group_message
            module.collaboration_hub.append_message = real_append_message

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(observed.get("statusBeforeRelay"), "done", payload)

        relay = payload.get("collabRelay") if isinstance(payload.get("collabRelay"), dict) else {}
        self.assertTrue(relay.get("ok"), payload)
        self.assertEqual(relay.get("threadId"), "T-223:coder", payload)
        self.assertEqual(relay.get("messageType"), "decision", payload)

        rows = self._read_collab_messages()
        self.assertTrue(rows, payload)
        row = rows[-1]
        self.assertEqual(row.get("threadId"), "T-223:coder", row)
        self.assertEqual(row.get("fromAgent"), "coder", row)
        self.assertEqual(row.get("toAgent"), "orchestrator", row)
        self.assertEqual(row.get("messageType"), "decision", row)

    def test_feishu_router_progress_wakeup_send_relay_persists_answer_payload(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-224: progress relay success",
        ])

        module = load_milestone_module()
        real_send_group_message = module.send_group_message
        real_dispatch_once = module.dispatch_once
        real_append_message = module.collaboration_hub.append_message
        dispatch_called = {"seen": False}
        observed = {}
        out_buffer = io.StringIO()
        args = argparse.Namespace(
            root=str(self.root),
            actor="coder",
            text="@orchestrator T-224 进展同步：主因已定位，正在补充验证数据",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode=module.DEFAULT_VISIBILITY_MODE,
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        def dispatch_once_with_marker(d_args):
            dispatch_called["seen"] = True
            return real_dispatch_once(d_args)

        def append_after_dispatch(root, payload, *extra_args, **extra_kwargs):
            observed["dispatchBeforeRelay"] = dispatch_called["seen"]
            return real_append_message(root, payload, *extra_args, **extra_kwargs)

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True, "dryRun": False}
            module.dispatch_once = dispatch_once_with_marker
            module.collaboration_hub.append_message = append_after_dispatch
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send_group_message
            module.dispatch_once = real_dispatch_once
            module.collaboration_hub.append_message = real_append_message

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("ok"), payload)
        self.assertTrue(observed.get("dispatchBeforeRelay"), payload)

        relay = payload.get("collabRelay") if isinstance(payload.get("collabRelay"), dict) else {}
        self.assertTrue(relay.get("ok"), payload)
        self.assertEqual(relay.get("threadId"), "T-224:coder", payload)
        self.assertEqual(relay.get("messageType"), "answer", payload)

        rows = self._read_collab_messages()
        self.assertTrue(rows, payload)
        row = rows[-1]
        self.assertEqual(row.get("threadId"), "T-224:coder", row)
        self.assertEqual(row.get("fromAgent"), "coder", row)
        self.assertEqual(row.get("toAgent"), "orchestrator", row)
        self.assertEqual(row.get("messageType"), "answer", row)

    def test_feishu_router_blocked_wakeup_relay_failure_in_send_mode_keeps_main_flow_success(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-225: blocked relay failure non-blocking",
        ])

        module = load_milestone_module()
        real_send_group_message = module.send_group_message
        real_append_message = module.collaboration_hub.append_message
        out_buffer = io.StringIO()
        args = argparse.Namespace(
            root=str(self.root),
            actor="coder",
            text="@orchestrator T-225 阻塞：依赖服务异常，无法继续",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode=module.DEFAULT_VISIBILITY_MODE,
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        def boom_after_main(root, _payload, *_args, **_kwargs):
            task = module.get_task(root, "T-225") or {}
            status = str(task.get("status") or "missing")
            raise RuntimeError(f"forced wakeup relay failure status={status}")

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True, "dryRun": False}
            module.collaboration_hub.append_message = boom_after_main
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send_group_message
            module.collaboration_hub.append_message = real_append_message

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("ok"), payload)
        relay = payload.get("collabRelay") if isinstance(payload.get("collabRelay"), dict) else {}
        self.assertIn("collabRelay", payload, payload)
        self.assertFalse(relay.get("ok", True), payload)
        self.assertEqual(relay.get("reason"), "append_exception", payload)
        self.assertEqual(relay.get("messageType"), "decision", payload)
        self.assertIn("status=blocked", str(relay.get("error") or ""), payload)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-225",
        ])
        self.assertEqual(status["task"]["status"], "blocked", status)

    def test_feishu_router_done_wakeup_relay_failure_in_send_mode_does_not_break_success(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-222: relay failure non-blocking",
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
            "@coder claim task T-222",
        ])

        module = load_milestone_module()
        real_send_group_message = module.send_group_message
        real_append_message = module.collaboration_hub.append_message
        out_buffer = io.StringIO()
        args = argparse.Namespace(
            root=str(self.root),
            actor="coder",
            text="@orchestrator T-222 已完成，证据: logs/t222.log",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode=module.DEFAULT_VISIBILITY_MODE,
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced wakeup relay failure")

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True, "dryRun": False}
            module.collaboration_hub.append_message = boom
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send_group_message
            module.collaboration_hub.append_message = real_append_message

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("ok"), payload)
        relay = payload.get("collabRelay") if isinstance(payload.get("collabRelay"), dict) else {}
        self.assertFalse(relay.get("ok", True), payload)
        self.assertEqual(relay.get("reason"), "append_exception", payload)

    def test_feishu_router_board_mark_done_cleans_only_target_task_state(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-002B: board done clean",
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
            "@coder create task T-002B-KEEP: board keep active",
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
            "@coder claim task T-002B",
        ])

        module = load_milestone_module()
        module.context_pack.record_failure(
            self.root.as_posix(),
            task_id="T-002B",
            agent="coder",
            executor="codex_cli",
            prompt_text="board prompt",
            output_text="board blocked",
            blocked_reason="spawn_failed",
        )
        module.context_pack.record_failure(
            self.root.as_posix(),
            task_id="T-002B-KEEP",
            agent="coder",
            executor="codex_cli",
            prompt_text="keep prompt",
            output_text="keep blocked",
            blocked_reason="spawn_failed",
        )
        now_ts = int(time.time())
        module.recovery_loop.decide_recovery(self.root.as_posix(), "T-002B", "coder", "spawn_failed", now_ts=now_ts)
        module.recovery_loop.decide_recovery(self.root.as_posix(), "T-002B-KEEP", "coder", "spawn_failed", now_ts=now_ts)
        module.session_registry.ensure_session(self.root.as_posix(), "T-002B", "coder", "codex_cli")
        module.session_registry.ensure_session(self.root.as_posix(), "T-002B-KEEP", "coder", "codex_cli")

        done = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator mark done T-002B: 已完成，测试通过，证据: logs/t002b.log",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(done["ok"], done)
        self.assertEqual(done.get("intent"), "board_cmd", done)
        self.assertEqual((done.get("apply") or {}).get("intent"), "mark_done", done)

        self.assertEqual(module.context_pack.build_retry_context(self.root.as_posix(), "T-002B"), {})
        self.assertTrue(module.context_pack.build_retry_context(self.root.as_posix(), "T-002B-KEEP"))
        self.assertEqual(module.recovery_loop.get_active_cooldown(self.root.as_posix(), "T-002B", now_ts=now_ts), {})
        self.assertTrue(module.recovery_loop.get_active_cooldown(self.root.as_posix(), "T-002B-KEEP", now_ts=now_ts))

        state = module.session_registry.load_registry(self.root.as_posix())
        sessions = state.get("sessions") or {}
        key_done = module.session_registry.session_key("T-002B", "coder", "codex_cli")
        key_keep = module.session_registry.session_key("T-002B-KEEP", "coder", "codex_cli")
        self.assertEqual((sessions.get(key_done) or {}).get("status"), "done", sessions)
        self.assertEqual((sessions.get(key_keep) or {}).get("status"), "active", sessions)

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

    def test_clarify_dry_run_skips_collaboration_question_log(self):
        out = run_json([
            "python3",
            str(MILE),
            "clarify",
            "--root",
            str(self.root),
            "--task-id",
            "T-003C",
            "--role",
            "debugger",
            "--question",
            "请给出最新错误栈",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        collab_log = out.get("collabLog") if isinstance(out.get("collabLog"), dict) else {}
        self.assertTrue(collab_log.get("skipped"), out)
        self.assertEqual(collab_log.get("reason"), "mode_not_send", out)
        self.assertEqual(collab_log.get("threadId"), "T-003C:debugger", out)

        collab_messages = self.root / "state" / "collab.messages.jsonl"
        self.assertFalse(collab_messages.exists(), out)

    def test_clarify_collaboration_log_failure_in_send_mode_does_not_break_success(self):
        module = load_milestone_module()
        real_send_group_message = module.send_group_message
        real_append_message = module.collaboration_hub.append_message
        out_buffer = io.StringIO()
        args = argparse.Namespace(
            root=str(self.root),
            task_id="T-003CF",
            role="debugger",
            question="请提供失败原因",
            actor="orchestrator",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            cooldown_sec=300,
            state_file="",
            mode="send",
            force=False,
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced collab append failure")

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True, "dryRun": False}
            module.collaboration_hub.append_message = boom
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_clarify(args)
        finally:
            module.send_group_message = real_send_group_message
            module.collaboration_hub.append_message = real_append_message

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("ok"), payload)
        collab_log = payload.get("collabLog") if isinstance(payload.get("collabLog"), dict) else {}
        self.assertFalse(collab_log.get("ok", True), payload)
        self.assertIn("reason", collab_log, payload)
        self.assertEqual(collab_log.get("reason"), "append_exception", payload)

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

    def test_intervene_task_script_set_show_clear(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040I0: intervention script coverage",
        ])

        set_out = run_json([
            "python3",
            str(INTERVENE),
            "set",
            "--root",
            str(self.root),
            "--task-id",
            "T-040I0",
            "--message",
            "Focus on API first.",
            "--actor",
            "orchestrator",
        ])
        self.assertTrue(set_out.get("ok"), set_out)
        intervention = set_out.get("intervention") or {}
        self.assertEqual(intervention.get("taskId"), "T-040I0", set_out)
        self.assertEqual(intervention.get("message"), "Focus on API first.", set_out)
        self.assertEqual(intervention.get("actor"), "orchestrator", set_out)
        self.assertEqual(intervention.get("applyCount"), 0, set_out)

        show_out = run_json([
            "python3",
            str(INTERVENE),
            "show",
            "--root",
            str(self.root),
            "--task-id",
            "T-040I0",
        ])
        self.assertTrue(show_out.get("ok"), show_out)
        self.assertTrue(show_out.get("active"), show_out)
        self.assertEqual((show_out.get("intervention") or {}).get("message"), "Focus on API first.", show_out)

        clear_out = run_json([
            "python3",
            str(INTERVENE),
            "clear",
            "--root",
            str(self.root),
            "--task-id",
            "T-040I0",
            "--actor",
            "orchestrator",
        ])
        self.assertTrue(clear_out.get("ok"), clear_out)
        self.assertTrue(clear_out.get("cleared"), clear_out)

        empty_out = run_json([
            "python3",
            str(INTERVENE),
            "show",
            "--root",
            str(self.root),
            "--task-id",
            "T-040I0",
        ])
        self.assertTrue(empty_out.get("ok"), empty_out)
        self.assertFalse(empty_out.get("active"), empty_out)

    def test_build_agent_prompt_injects_intervention_context(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040I1: intervention prompt injection",
        ])

        module = load_milestone_module()
        module.set_task_intervention(
            self.root.as_posix(),
            "T-040I1",
            "Focus on API first.",
            actor="orchestrator",
        )
        task = module.get_task(self.root.as_posix(), "T-040I1")
        prompt = module.build_agent_prompt(
            self.root.as_posix(),
            task,
            "coder",
            "T-040I1: intervention prompt injection",
        )
        self.assertIn("INTERVENTION_CONTEXT", prompt)
        self.assertIn("Focus on API first.", prompt)
        self.assertIn('"applyCount": 1', prompt)

        state = self._read_interventions_state()
        entry = ((state.get("tasks") or {}).get("T-040I1") or {})
        self.assertEqual(entry.get("applyCount"), 1, state)

    def test_build_agent_prompt_omits_intervention_context_when_missing(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040I2: no intervention prompt compatibility",
        ])

        module = load_milestone_module()
        task = module.get_task(self.root.as_posix(), "T-040I2")
        prompt = module.build_agent_prompt(
            self.root.as_posix(),
            task,
            "coder",
            "T-040I2: no intervention prompt compatibility",
        )
        self.assertNotIn("INTERVENTION_CONTEXT", prompt)

    def test_dispatch_prompt_injects_intervention_and_updates_apply_count(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040I3: dispatch intervention apply count",
        ])

        module = load_milestone_module()
        module.set_task_intervention(
            self.root.as_posix(),
            "T-040I3",
            "Focus on API first.",
            actor="orchestrator",
        )

        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040I3",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"已完成并验证","evidence":["logs/t040i3.log","pytest passed"]}',
        ])
        self.assertTrue(out.get("ok"), out)
        self.assertIn("INTERVENTION_CONTEXT", out.get("agentPrompt", ""), out)

        state = self._read_interventions_state()
        entry = ((state.get("tasks") or {}).get("T-040I3") or {})
        self.assertEqual(entry.get("applyCount"), 1, state)

    def test_feishu_router_supports_intervention_commands(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040I4: intervention command routing",
        ])

        set_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator intervene T-040I4: Focus on API first.",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(set_out.get("ok"), set_out)
        self.assertEqual(set_out.get("intent"), "intervene", set_out)
        self.assertEqual((set_out.get("intervention") or {}).get("message"), "Focus on API first.", set_out)
        self.assertTrue(((set_out.get("send") or {}).get("payload") or {}).get("text"), set_out)

        show_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator intervention T-040I4",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(show_out.get("ok"), show_out)
        self.assertEqual(show_out.get("intent"), "intervention", show_out)
        self.assertTrue(show_out.get("active"), show_out)
        self.assertIn("Focus on API first.", ((show_out.get("send") or {}).get("payload") or {}).get("text", ""), show_out)

        clear_out = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@orchestrator clear intervention T-040I4",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(clear_out.get("ok"), clear_out)
        self.assertEqual(clear_out.get("intent"), "clear_intervention", clear_out)
        self.assertTrue(clear_out.get("cleared"), clear_out)

        state = self._read_interventions_state()
        self.assertNotIn("T-040I4", state.get("tasks") or {}, state)

    def test_build_agent_prompt_injects_business_context(self):
        import sqlite3

        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040B1: business context prompt injection",
        ])

        self._write_json_file(
            "state/task-context-map.json",
            {
                "tasks": {
                    "T-040B1": {
                        "projectPath": str(self.root),
                        "projectName": "runtime-test",
                        "dispatchPrompt": "use customer + paper context",
                        "customerId": "cust-a",
                        "paperId": "paper-a",
                    }
                }
            },
        )

        db_path = self.root / "state" / "business_context.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE customers (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    requirements TEXT,
                    tech_stack TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE papers (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    authors TEXT,
                    arxiv_id TEXT,
                    difficulty_score REAL,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE reproduction_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    paper_id TEXT,
                    success INTEGER,
                    issues TEXT,
                    lessons_learned TEXT,
                    created_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO customers(id, name, requirements, tech_stack, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("cust-a", "ACME", "Need weekly reports", "Python,SQLite", "2026-03-06T00:00:00Z", "2026-03-06T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO papers(id, title, authors, arxiv_id, difficulty_score, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("paper-a", "Test Paper", "Alice;Bob", "2501.00001", 0.7, "2026-03-06T00:00:00Z", "2026-03-06T00:00:00Z"),
            )
            conn.execute(
                "INSERT INTO reproduction_history(paper_id, success, issues, lessons_learned, created_at) VALUES (?, ?, ?, ?, ?)",
                ("paper-a", 1, "none", "use smaller batch", "2026-03-06T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

        module = load_milestone_module()
        task = module.get_task(self.root.as_posix(), "T-040B1")
        prompt = module.build_agent_prompt(
            self.root.as_posix(),
            task,
            "coder",
            "T-040B1: business context prompt injection",
        )
        self.assertIn("BUSINESS_CONTEXT", prompt)
        self.assertIn("ACME", prompt)
        self.assertIn("Test Paper", prompt)
        self.assertIn("use smaller batch", prompt)

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

    def test_dispatch_prompt_includes_collab_thread_summary_when_available(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040C: 协作线程摘要注入",
        ])

        module = load_milestone_module()
        thread_id = module.collaboration_thread_id("T-040C", "coder")
        append = module.collaboration_hub.append_message(
            self.root.as_posix(),
            {
                "taskId": "T-040C",
                "threadId": thread_id,
                "fromAgent": "orchestrator",
                "toAgent": "coder",
                "messageType": "question",
                "summary": "请先确认测试边界",
                "evidence": ["clarify dry-run"],
                "request": "补充输入边界并回传日志",
                "deadline": module.now_iso(),
                "createdAt": module.now_iso(),
            },
        )
        self.assertTrue(append.get("ok"), append)

        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040C",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertIn("COLLAB_THREAD_SUMMARY", prompt, out)
        self.assertIn('"threadId": "T-040C:coder"', prompt, out)
        self.assertIn('"messageCount": 1', prompt, out)

    def test_dispatch_prompt_degrades_when_collab_summary_raises(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040CE: 协作摘要异常降级",
        ])

        module = load_milestone_module()
        real_summarize_thread = module.collaboration_hub.summarize_thread

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced summarize failure")

        args = module.build_parser().parse_args(
            [
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                "T-040CE",
                "--agent",
                "coder",
                "--mode",
                "dry-run",
            ]
        )

        try:
            module.collaboration_hub.summarize_thread = boom
            out = module.dispatch_once(args)
        finally:
            module.collaboration_hub.summarize_thread = real_summarize_thread

        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertNotIn("COLLAB_THREAD_SUMMARY", prompt, out)
        collab = out.get("collaboration") or {}
        self.assertFalse(collab.get("available", True), out)
        self.assertEqual(collab.get("reason"), "summary_read_failed", out)
        self.assertIn("forced summarize failure", str(collab.get("error") or ""), out)

    def test_dispatch_prompt_degrades_when_collab_summary_invalid(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040CI: 协作摘要非法值降级",
        ])

        module = load_milestone_module()
        real_summarize_thread = module.collaboration_hub.summarize_thread
        args = module.build_parser().parse_args(
            [
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                "T-040CI",
                "--agent",
                "coder",
                "--mode",
                "dry-run",
            ]
        )

        try:
            module.collaboration_hub.summarize_thread = lambda *_args, **_kwargs: "invalid-summary"
            out = module.dispatch_once(args)
        finally:
            module.collaboration_hub.summarize_thread = real_summarize_thread

        self.assertTrue(out["ok"], out)
        prompt = out.get("agentPrompt", "")
        self.assertNotIn("COLLAB_THREAD_SUMMARY", prompt, out)
        collab = out.get("collaboration") or {}
        self.assertFalse(collab.get("available", True), out)
        self.assertEqual(collab.get("reason"), "summary_invalid", out)

    def test_dispatch_sets_escalation_when_collaboration_round_limit_hit(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040ER: 协作轮次阈值升级",
        ])

        module = load_milestone_module()
        thread_id = module.collaboration_thread_id("T-040ER", "coder")
        for index in range(3):
            appended = module.collaboration_hub.append_message(
                self.root.as_posix(),
                {
                    "taskId": "T-040ER",
                    "threadId": thread_id,
                    "fromAgent": "orchestrator",
                    "toAgent": "coder",
                    "messageType": "question",
                    "summary": f"第{index + 1}轮确认",
                    "evidence": [f"q-{index + 1}"],
                    "request": f"请更新第{index + 1}轮结果",
                    "deadline": module.now_iso(),
                    "createdAt": f"2026-03-04T09:{10 + index:02d}:00Z",
                },
            )
            self.assertTrue(appended.get("ok"), appended)

        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040ER",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        collab = out.get("collaboration") if isinstance(out.get("collaboration"), dict) else {}
        escalation = collab.get("escalation") if isinstance(collab.get("escalation"), dict) else {}
        self.assertTrue(escalation.get("required"), out)
        self.assertEqual(escalation.get("reason"), "round_limit", out)
        self.assertIn("协作线程已触发升级门槛", out.get("agentPrompt", ""), out)

        task_text = (((out.get("taskSend") or {}).get("payload") or {}).get("text") or "")
        self.assertIn("协作线程已触发升级门槛", task_text, out)

    def test_dispatch_sets_escalation_when_collaboration_timeout_hit(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040ET: 协作超时升级",
        ])

        module = load_milestone_module()
        thread_id = module.collaboration_thread_id("T-040ET", "coder")
        appended = module.collaboration_hub.append_message(
            self.root.as_posix(),
            {
                "taskId": "T-040ET",
                "threadId": thread_id,
                "fromAgent": "orchestrator",
                "toAgent": "coder",
                "messageType": "answer",
                "summary": "上次同步",
                "evidence": ["logs/timeout.log"],
                "request": "等待下一次回传",
                "deadline": module.now_iso(),
                "createdAt": "2020-03-04T09:00:00Z",
            },
        )
        self.assertTrue(appended.get("ok"), appended)

        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040ET",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out.get("ok"), out)
        collab = out.get("collaboration") if isinstance(out.get("collaboration"), dict) else {}
        escalation = collab.get("escalation") if isinstance(collab.get("escalation"), dict) else {}
        self.assertTrue(escalation.get("required"), out)
        self.assertEqual(escalation.get("reason"), "timeout", out)
        self.assertIn("协作线程已触发升级门槛", out.get("agentPrompt", ""), out)

        task_text = (((out.get("taskSend") or {}).get("payload") or {}).get("text") or "")
        self.assertIn("协作线程已触发升级门槛", task_text, out)

    def test_dispatch_prompt_injects_retry_context_pack(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040R: 重试上下文注入测试",
        ])

        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040R",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"message":"[BLOCKED] waiting upstream data"}',
        ])
        self.assertEqual((first.get("spawn") or {}).get("reasonCode"), "blocked_signal", first)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-040R",
            "--agent",
            "debugger",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"done","summary":"done with evidence","evidence":["logs/t040r.log"]}',
        ])
        self.assertTrue(second["ok"], second)
        prompt = second.get("agentPrompt", "")
        self.assertIn("RETRY_CONTEXT_PACK", prompt, second)
        self.assertIn("blockedReason", prompt, second)
        retry_context = second.get("retryContext") or {}
        self.assertEqual(retry_context.get("blockedReason"), "blocked_signal", second)

    def test_inline_retry_uses_final_blocked_snapshot(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040R2: 二次阻塞快照覆盖",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        call_state = {"count": 0}

        first_spawn = {
            "ok": True,
            "skipped": False,
            "decision": "blocked",
            "reasonCode": "incomplete_output",
            "detail": "first blocked missing evidence",
            "stdout": '{"status":"done","summary":"阶段进度"}',
            "stderr": "",
            "command": ["mock-first"],
            "executor": "codex_cli",
            "metrics": {"elapsedMs": 11, "tokenUsage": 5},
        }
        second_spawn = {
            "ok": True,
            "skipped": False,
            "decision": "blocked",
            "reasonCode": "spawn_failed",
            "detail": "second blocked by runtime error",
            "stdout": "Traceback runtime error on retry",
            "stderr": "",
            "command": ["mock-second"],
            "executor": "codex_cli",
            "metrics": {"elapsedMs": 13, "tokenUsage": 7},
        }

        def fake_run_dispatch_spawn(_args, _prompt):
            call_state["count"] += 1
            return dict(first_spawn if call_state["count"] == 1 else second_spawn)

        args = argparse.Namespace(
            root=self.root.as_posix(),
            task_id="T-040R2",
            agent="coder",
            task="T-040R2: 二次阻塞快照覆盖",
            actor="orchestrator",
            session_id="",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=120,
            spawn=True,
            spawn_cmd="",
            spawn_output="",
            visibility_mode="handoff_visible",
        )

        try:
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn

        self.assertTrue(out["ok"], out)
        self.assertEqual(call_state["count"], 2, out)
        self.assertTrue((out.get("spawn") or {}).get("retried"), out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "spawn_failed", out)
        self.assertEqual((out.get("spawn") or {}).get("detail"), "second blocked by runtime error", out)

        retry_context = (out.get("spawn") or {}).get("retryContext") or {}
        self.assertEqual(retry_context.get("blockedReason"), "spawn_failed", out)
        self.assertTrue(retry_context.get("lastOutputDigest"), out)
        recent = retry_context.get("recentDecisions") or []
        self.assertTrue(recent, out)
        self.assertEqual((recent[-1] or {}).get("reasonCode"), "spawn_failed", out)

    def test_inline_retry_record_failure_exception_does_not_break_main_path(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-040R3: inline retry 记录失败容错",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        real_record_failure = module.context_pack.record_failure
        call_state = {"count": 0}

        first_spawn = {
            "ok": True,
            "skipped": False,
            "decision": "blocked",
            "reasonCode": "incomplete_output",
            "detail": "need stronger evidence",
            "stdout": '{"status":"done","summary":"阶段进度"}',
            "stderr": "",
            "command": ["mock-first"],
            "executor": "claude_cli",
            "metrics": {"elapsedMs": 9, "tokenUsage": 4},
        }
        second_spawn = {
            "ok": True,
            "skipped": False,
            "decision": "done",
            "reasonCode": "done_with_evidence",
            "detail": "retry success with logs",
            "stdout": '{"status":"done","summary":"done","evidence":["logs/t040r3.log"]}',
            "stderr": "",
            "command": ["mock-second"],
            "executor": "claude_cli",
            "metrics": {"elapsedMs": 12, "tokenUsage": 6},
        }

        def fake_run_dispatch_spawn(_args, _prompt):
            call_state["count"] += 1
            return dict(first_spawn if call_state["count"] == 1 else second_spawn)

        def fake_record_failure(*_args, **_kwargs):
            raise RuntimeError("forced inline record failure")

        args = argparse.Namespace(
            root=self.root.as_posix(),
            task_id="T-040R3",
            agent="coder",
            task="T-040R3: inline retry 容错",
            actor="orchestrator",
            session_id="",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=120,
            spawn=True,
            spawn_cmd="",
            spawn_output="",
            visibility_mode="handoff_visible",
        )

        try:
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            module.context_pack.record_failure = fake_record_failure
            with self.assertLogs(module.__name__, level="WARNING") as logs:
                out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn
            module.context_pack.record_failure = real_record_failure

        self.assertTrue(out["ok"], out)
        self.assertEqual(call_state["count"], 2, out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "done", out)
        self.assertTrue(any("inline retry" in line.lower() for line in logs.output), logs.output)

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

    def test_dispatch_checkpoint_progress_continues_without_recovery(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-CP1: continuation happy path",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP1",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"still running","checkpoint":{"progressPercent":20,"completed":["mapped root cause"],"remaining":["patch classifier"],"nextAction":"patch milestones classifier","continueHint":"continue","stallSignal":"none","evidenceDelta":["found no_completion_signal fallthrough"]}}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("decision"), "continue", out)
        self.assertEqual(spawn.get("reasonCode"), "checkpoint_continue", out)
        self.assertEqual((out.get("closeApply") or {}).get("intent"), "claim_task", out)
        self.assertNotIn(spawn.get("action"), {"retry", "human", "escalate"}, out)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-CP1",
        ])
        self.assertEqual(status["task"]["status"], "in_progress", status)
        self.assertEqual(status["task"]["owner"], "coder", status)

        cont_state = json.loads((self.root / "state" / "continuation.state.json").read_text(encoding="utf-8"))
        row = ((cont_state.get("tasks") or {}).get("T-CP1")) or {}
        self.assertEqual(int(row.get("rounds") or 0), 1, cont_state)
        self.assertGreaterEqual(int(row.get("firstTs") or 0), 1, cont_state)
        self.assertGreaterEqual(int(row.get("lastTs") or 0), int(row.get("firstTs") or 0), cont_state)
        self.assertEqual(int(row.get("lastProgressPercent") or -1), 20, cont_state)
        self.assertEqual(int(row.get("noProgressStreak", -1)), 0, cont_state)
        self.assertIsInstance(row.get("evidenceSet"), list, cont_state)
        self.assertTrue(str(row.get("evidenceHash") or "").strip(), cont_state)

    def test_dispatch_progress_without_checkpoint_uses_legacy_continuation_heuristic(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-CP1B: continuation legacy fallback",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP1B",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"still running, collecting logs and preparing patch","evidence":["logs/t-cp1b.log"],"nextActions":["patch milestones classifier"]}',
        ])
        self.assertTrue(out["ok"], out)
        spawn = out.get("spawn") or {}
        self.assertEqual(spawn.get("decision"), "continue", out)
        self.assertEqual(spawn.get("reasonCode"), "legacy_progress_continue", out)

        cont_state = json.loads((self.root / "state" / "continuation.state.json").read_text(encoding="utf-8"))
        row = ((cont_state.get("tasks") or {}).get("T-CP1B")) or {}
        self.assertEqual(int(row.get("rounds") or 0), 1, cont_state)
        self.assertEqual(int(row.get("lastProgressPercent")) if row.get("lastProgressPercent") is not None else -1, 0, cont_state)

    def test_dispatch_progress_without_checkpoint_explicit_blocked_stays_blocked(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-CP1C: continuation fallback must keep blocked",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP1C",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"blocked by missing SECRET_KEY from upstream env"}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "blocked_signal", out)

    def test_dispatch_checkpoint_enabled_zero_disables_continuation(self):
        self._write_json_file(
            "config/runtime-policy.json",
            {
                "orchestrator": {
                    "continuationPolicy": {
                        "enabled": 0,
                        "maxContinuationRounds": 6,
                        "noProgressWindowRounds": 2,
                        "minProgressDeltaPct": 3,
                        "minEvidenceDeltaItems": 1,
                        "maxContinuationWallTimeSec": 1800,
                    }
                }
            },
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
            "@coder create task T-CP1D: continuation disabled by numeric zero",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP1D",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round1","checkpoint":{"progressPercent":10,"completed":["step1"],"remaining":["step2"],"nextAction":"continue","continueHint":"continue","stallSignal":"none","evidenceDelta":["first evidence"]}}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "no_completion_signal", out)
        path = self.root / "state" / "continuation.state.json"
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("T-CP1D", (state.get("tasks") or {}), state)

    def test_continuation_policy_normalizer_honors_numeric_zero_enabled(self):
        module = load_milestone_module()
        normalized = module._normalize_continuation_policy({"enabled": 0})
        self.assertEqual(normalized.get("enabled"), False, normalized)

    def test_checkpoint_continuation_same_key_multi_process_counts_all_rounds(self):
        workers = 6
        rounds = 6
        expected_rounds = workers * rounds

        self._write_json_file(
            "config/runtime-policy.json",
            {
                "orchestrator": {
                    "continuationPolicy": {
                        "enabled": True,
                        "maxContinuationRounds": expected_rounds + 10,
                        "noProgressWindowRounds": expected_rounds + 10,
                        "minProgressDeltaPct": 0,
                        "minEvidenceDeltaItems": 0,
                        "maxContinuationWallTimeSec": 0,
                    }
                }
            },
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = []
        for _ in range(workers):
            process = ctx.Process(
                target=_continuation_same_key_worker,
                args=(self.root.as_posix(), rounds, start_event, result_queue),
            )
            process.start()
            processes.append(process)

        start_event.set()
        for process in processes:
            process.join(timeout=90)
            self.assertFalse(process.is_alive(), f"worker did not finish: pid={process.pid}")
            self.assertEqual(process.exitcode, 0, f"worker exit code mismatch: pid={process.pid}")

        worker_results = [result_queue.get(timeout=5) for _ in range(workers)]
        failures = [item for item in worker_results if not item.get("ok")]
        self.assertEqual(failures, [], worker_results)

        state_path = self.root / "state" / "continuation.state.json"
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        row = ((loaded.get("tasks") or {}).get("T-CP-LOCK")) or {}
        self.assertEqual(int(row.get("rounds") or 0), expected_rounds, loaded)

    def test_dispatch_checkpoint_need_input_blocks(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-CP2: continuation need input",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP2",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"need user choice","checkpoint":{"progressPercent":25,"completed":["triaged branch"],"remaining":["decide deployment target"],"nextAction":"wait for user selection","continueHint":"need_input","stallSignal":"none","evidenceDelta":["two rollout options prepared"]}}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "continuation_need_input", out)

    def test_dispatch_checkpoint_hard_block_stays_blocked(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-CP3: continuation hard block",
        ])
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP3",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"blocked by upstream secret","checkpoint":{"progressPercent":15,"completed":["validated env"],"remaining":["fetch missing secret"],"nextAction":"request secret access","continueHint":"continue","stallSignal":"hard_block","evidenceDelta":["missing SECRET_KEY in env"]}}',
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "blocked_signal", out)

    def test_dispatch_checkpoint_round_limit_blocks(self):
        self._write_json_file(
            "config/runtime-policy.json",
            {
                "orchestrator": {
                    "continuationPolicy": {
                        "enabled": True,
                        "maxContinuationRounds": 1,
                        "noProgressWindowRounds": 2,
                        "minProgressDeltaPct": 3,
                        "minEvidenceDeltaItems": 1,
                        "maxContinuationWallTimeSec": 1800,
                    }
                }
            },
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
            "@coder create task T-CP4: continuation round limit",
        ])
        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP4",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round1","checkpoint":{"progressPercent":10,"completed":["step1"],"remaining":["step2"],"nextAction":"continue","continueHint":"continue","stallSignal":"none","evidenceDelta":["first evidence"]}}',
        ])
        self.assertEqual((first.get("spawn") or {}).get("decision"), "continue", first)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP4",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round2","checkpoint":{"progressPercent":40,"completed":["step2"],"remaining":[],"nextAction":"finalize","continueHint":"continue","stallSignal":"none","evidenceDelta":["second evidence"]}}',
        ])
        self.assertEqual((second.get("spawn") or {}).get("decision"), "blocked", second)
        self.assertEqual((second.get("spawn") or {}).get("reasonCode"), "continuation_round_limit", second)

    def test_dispatch_checkpoint_no_progress_window_blocks(self):
        self._write_json_file(
            "config/runtime-policy.json",
            {
                "orchestrator": {
                    "continuationPolicy": {
                        "enabled": True,
                        "maxContinuationRounds": 6,
                        "noProgressWindowRounds": 1,
                        "minProgressDeltaPct": 3,
                        "minEvidenceDeltaItems": 1,
                        "maxContinuationWallTimeSec": 1800,
                    }
                }
            },
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
            "@coder create task T-CP5: continuation no progress",
        ])
        first = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP5",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round1","checkpoint":{"progressPercent":12,"completed":["step1"],"remaining":["step2"],"nextAction":"continue","continueHint":"continue","stallSignal":"none","evidenceDelta":["first evidence"]}}',
        ])
        self.assertEqual((first.get("spawn") or {}).get("decision"), "continue", first)

        second = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP5",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round2","checkpoint":{"progressPercent":12,"completed":["step1"],"remaining":["step2"],"nextAction":"continue","continueHint":"continue","stallSignal":"none","evidenceDelta":[]}}',
        ])
        self.assertEqual((second.get("spawn") or {}).get("decision"), "blocked", second)
        self.assertEqual((second.get("spawn") or {}).get("reasonCode"), "continuation_no_progress", second)

    def test_dispatch_checkpoint_timeout_blocks(self):
        self._write_json_file(
            "config/runtime-policy.json",
            {
                "orchestrator": {
                    "continuationPolicy": {
                        "enabled": True,
                        "maxContinuationRounds": 6,
                        "noProgressWindowRounds": 2,
                        "minProgressDeltaPct": 3,
                        "minEvidenceDeltaItems": 1,
                        "maxContinuationWallTimeSec": 1,
                    }
                }
            },
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
            "@coder create task T-CP6: continuation timeout",
        ])
        self._write_json_file(
            "state/continuation.state.json",
            {
                "tasks": {
                    "T-CP6": {
                        "rounds": 1,
                        "firstTs": 1,
                        "lastTs": 1,
                        "lastProgressPercent": 10,
                        "evidenceSet": ["old-evidence"],
                        "evidenceHash": "old-hash",
                        "noProgressStreak": 0,
                    }
                }
            },
        )
        out = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-CP6",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn",
            "--spawn-output",
            '{"status":"progress","summary":"round2","checkpoint":{"progressPercent":40,"completed":["step2"],"remaining":[],"nextAction":"finalize","continueHint":"continue","stallSignal":"none","evidenceDelta":["new evidence"]}}',
        ])
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual((out.get("spawn") or {}).get("reasonCode"), "continuation_timeout", out)

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

    def test_feishu_router_autopilot_send_mode_starts_background_runner(self):
        module = load_milestone_module()
        real_send = module.send_group_message
        real_popen = module.subprocess.Popen
        real_cmd_autopilot = module.cmd_autopilot
        out_buffer = io.StringIO()
        popen_calls = {"count": 0, "cmd": []}

        class FakePopen:
            def __init__(self, pid: int):
                self.pid = pid

        def fake_popen(cmd, stdout=None, stderr=None, start_new_session=False):
            popen_calls["count"] += 1
            popen_calls["cmd"] = list(cmd)
            self.assertTrue(start_new_session)
            return FakePopen(55221)

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator autopilot 4",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode="handoff_visible",
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True}
            module.subprocess.Popen = fake_popen
            module.cmd_autopilot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("feishu-router autopilot should not call cmd_autopilot synchronously in send mode")
            )
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send
            module.subprocess.Popen = real_popen
            module.cmd_autopilot = real_cmd_autopilot

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertEqual(payload.get("intent"), "autopilot", payload)
        run = payload.get("run") or {}
        self.assertTrue(run.get("async"), payload)
        self.assertEqual(run.get("status"), "started", payload)
        self.assertEqual(run.get("maxSteps"), 4, payload)
        self.assertEqual(popen_calls["count"], 1, payload)
        self.assertIn("autopilot-runner", " ".join(popen_calls["cmd"]), payload)

    def test_feishu_router_autopilot_send_mode_skips_when_already_running(self):
        module = load_milestone_module()
        real_send = module.send_group_message
        real_popen = module.subprocess.Popen
        real_cmd_autopilot = module.cmd_autopilot
        out_buffer = io.StringIO()

        state_path = self.root / "state" / "autopilot.runtime.json"
        state_path.write_text(
            json.dumps(
                {
                    "running": True,
                    "pid": os.getpid(),
                    "startedAt": "2026-03-04T00:00:00Z",
                    "updatedAt": "2026-03-04T00:00:00Z",
                    "maxSteps": 4,
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="utf-8",
        )

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator autopilot 4",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode="handoff_visible",
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True}
            module.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("already running autopilot should not start a new subprocess")
            )
            module.cmd_autopilot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("send mode should use async autopilot path")
            )
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send
            module.subprocess.Popen = real_popen
            module.cmd_autopilot = real_cmd_autopilot

        self.assertEqual(rc, 0)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertEqual(payload.get("intent"), "autopilot", payload)
        run = payload.get("run") or {}
        self.assertTrue(run.get("async"), payload)
        self.assertEqual(run.get("status"), "already_running", payload)
        self.assertTrue(run.get("skipped"), payload)

    def test_feishu_router_autopilot_send_mode_reports_spawn_failure_without_crash(self):
        module = load_milestone_module()
        real_send = module.send_group_message
        real_popen = module.subprocess.Popen
        real_cmd_autopilot = module.cmd_autopilot
        out_buffer = io.StringIO()

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator autopilot 4",
            group_id="oc_041146c92a9ccb403a7f4f48fb59701d",
            account_id="orchestrator",
            mode="send",
            session_id="",
            timeout_sec=0,
            dispatch_spawn=False,
            dispatch_manual=False,
            visibility_mode="handoff_visible",
            autopilot_max_steps=3,
            spawn_cmd="",
            spawn_output="",
            clarify_cooldown_sec=300,
            clarify_state_file="",
        )

        try:
            module.send_group_message = lambda *_args, **_kwargs: {"ok": True}
            module.subprocess.Popen = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn unavailable"))
            module.cmd_autopilot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("send mode should use async autopilot path")
            )
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.send_group_message = real_send
            module.subprocess.Popen = real_popen
            module.cmd_autopilot = real_cmd_autopilot

        self.assertEqual(rc, 1)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertTrue(payload.get("handled"), payload)
        self.assertEqual(payload.get("intent"), "autopilot", payload)
        run = payload.get("run") or {}
        self.assertEqual(run.get("status"), "failed_to_start", payload)
        self.assertIn("spawn unavailable", str(run.get("error") or ""), payload)

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

    def test_scheme_b_code_task_prefers_codex_executor(self):
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
        self.assertEqual(spawn.get("executor"), "codex_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("codex_worker_bridge.py" in str(x) for x in planned), out)
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

    def test_writing_task_forces_gemini_executor_even_for_debugger(self):
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
        self.assertEqual(spawn.get("executor"), "gemini_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("gemini_worker_bridge.py" in str(x) for x in planned), out)

    def test_planning_task_forces_claude_executor(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-062P: 请先规划系统架构方案与执行路线",
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
            "T-062P",
            "--agent",
            "coder",
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

    def test_scheme_b_other_roles_default_to_codex_executor(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@invest-analyst create task T-062: 其他角色默认走 codex",
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
        self.assertEqual(spawn.get("executor"), "codex_cli", out)
        planned = spawn.get("plannedCommand") or []
        self.assertTrue(any("codex_worker_bridge.py" in str(x) for x in planned), out)

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

    def test_scheduler_run_reclaims_stale_active_session_before_tick(self):
        module = load_milestone_module()
        module.session_registry.upsert_active_session(
            self.root.as_posix(),
            "T-SCHED-WD-1",
            worktree_path=(self.root / "task-worktrees" / "task-T-SCHED-WD-1").as_posix(),
            pid=987654,
            tmux_session="agent-T-SCHED-WD-1",
            status="running",
        )
        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            action="tick",
            interval_sec=None,
            max_steps=None,
            force=True,
            group_id="oc_test",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=0,
            spawn=False,
            spawn_cmd="",
            spawn_output="",
            visibility_mode="handoff_visible",
            session_id="",
        )

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(module.session_registry, "_pid_exists", return_value=False))
            out = module.scheduler_run_once(args)

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("intent"), "scheduler_run", out)
        watchdog = out.get("watchdog") or {}
        self.assertTrue(watchdog.get("ok"), out)
        self.assertEqual(watchdog.get("updated"), 1, out)
        self.assertEqual(watchdog.get("stalePid"), 1, out)
        events = watchdog.get("events") or []
        self.assertEqual(len(events), 1, out)
        self.assertEqual(events[0].get("taskId"), "T-SCHED-WD-1", out)
        self.assertEqual(events[0].get("reason"), "stale_pid", out)

        active_state = module.session_registry.load_active_sessions(self.root.as_posix())
        active_row = ((active_state.get("sessions") or {}).get("T-SCHED-WD-1")) or {}
        self.assertEqual(active_row.get("status"), "blocked", active_state)
        self.assertEqual(active_row.get("stopReason"), "stale_pid", active_state)

        metrics_path = self.root / "state" / "ops.metrics.jsonl"
        self.assertTrue(metrics_path.exists(), out)
        metric_rows = [
            json.loads(line)
            for line in metrics_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        matched = [
            row
            for row in metric_rows
            if row.get("event") == "active_session_watchdog" and row.get("taskId") == "T-SCHED-WD-1"
        ]
        self.assertEqual(len(matched), 1, metric_rows)
        self.assertEqual(matched[0].get("reason"), "stale_pid", matched)

    def test_scheduler_run_watchdog_exception_does_not_break_main_flow(self):
        module = load_milestone_module()
        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            action="tick",
            interval_sec=None,
            max_steps=None,
            force=True,
            group_id="oc_test",
            account_id="orchestrator",
            mode="dry-run",
            timeout_sec=0,
            spawn=False,
            spawn_cmd="",
            spawn_output="",
            visibility_mode="handoff_visible",
            session_id="",
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("watchdog exploded")

        real_watchdog = module.session_registry.run_active_session_watchdog
        try:
            module.session_registry.run_active_session_watchdog = boom
            out = module.scheduler_run_once(args)
        finally:
            module.session_registry.run_active_session_watchdog = real_watchdog

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("intent"), "scheduler_run", out)
        watchdog = out.get("watchdog") or {}
        self.assertFalse(watchdog.get("ok"), out)
        self.assertEqual(watchdog.get("reason"), "exception", out)
        self.assertIn("watchdog exploded", str(watchdog.get("error") or ""), out)

    def test_scheduler_run_scanner_disabled_returns_skip_summary(self):
        module = load_milestone_module()
        self._write_json_file(
            "config/scanner-policy.json",
            {
                "enabled": False,
                "dryRun": False,
                "todoComments": {"enabled": True, "paths": ["scripts"]},
                "pytestFailures": {"enabled": True, "logPath": "state/pytest.latest.log"},
                "feishuMessages": {"enabled": True, "messagesPath": "state/feishu.messages.json"},
                "arxivRss": {"enabled": False},
            },
        )

        with mock.patch.object(
            module,
            "autopilot_once",
            return_value={"ok": True, "skipped": True, "reason": "scanner_test", "stepsRun": 0},
        ):
            out = module.scheduler_run_once(self._scheduler_args(action="enable"))

        self.assertTrue(out.get("ok"), out)
        scanner = out.get("scanner") or {}
        self.assertEqual(scanner.get("checked"), 0, out)
        self.assertEqual(scanner.get("findings"), 0, out)
        self.assertEqual(scanner.get("created"), 0, out)
        self.assertEqual(scanner.get("reason"), "disabled", out)

    def test_scheduler_run_scanner_dry_run_reports_findings_without_creating_tasks(self):
        module = load_milestone_module()
        src = self.root / "scripts" / "demo.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("def f():\n    pass  # TODO: add retry\n", encoding="utf-8")
        (self.root / "state" / "pytest.latest.log").write_text(
            "FAILED tests/test_demo.py::test_abc - AssertionError: expected x\n",
            encoding="utf-8",
        )
        self._write_json_file(
            "state/feishu.messages.json",
            [{"text": "这个需求有变更，接口要改成批量版本"}],
        )
        self._write_json_file(
            "config/scanner-policy.json",
            {
                "enabled": True,
                "dryRun": True,
                "todoComments": {"enabled": True, "paths": ["scripts"]},
                "pytestFailures": {"enabled": True, "logPath": "state/pytest.latest.log"},
                "feishuMessages": {"enabled": True, "messagesPath": "state/feishu.messages.json"},
                "arxivRss": {"enabled": False},
            },
        )

        with mock.patch.object(
            module,
            "autopilot_once",
            return_value={"ok": True, "skipped": True, "reason": "scanner_test", "stepsRun": 0},
        ):
            out = module.scheduler_run_once(self._scheduler_args())

        scanner = out.get("scanner") or {}
        self.assertTrue(out.get("ok"), out)
        self.assertGreaterEqual(int(scanner.get("findings") or 0), 3, out)
        self.assertEqual(scanner.get("created"), 0, out)
        self.assertTrue(scanner.get("dryRun"), out)
        snapshot = self._read_task_snapshot()
        self.assertEqual(len((snapshot.get("tasks") or {})), 0, snapshot)

    def test_scheduler_run_scanner_creates_tasks_for_multiple_sources(self):
        module = load_milestone_module()
        src = self.root / "scripts" / "demo.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("def f():\n    pass  # TODO: add retry\n", encoding="utf-8")
        (self.root / "state" / "pytest.latest.log").write_text(
            "FAILED tests/test_demo.py::test_abc - AssertionError: expected x\n",
            encoding="utf-8",
        )
        self._write_json_file(
            "state/feishu.messages.json",
            [{"text": "这个需求有变更，接口要改成批量版本"}],
        )
        self._write_json_file(
            "config/scanner-policy.json",
            {
                "enabled": True,
                "dryRun": False,
                "todoComments": {"enabled": True, "paths": ["scripts"]},
                "pytestFailures": {"enabled": True, "logPath": "state/pytest.latest.log"},
                "feishuMessages": {"enabled": True, "messagesPath": "state/feishu.messages.json"},
                "arxivRss": {"enabled": True, "feedUrl": "https://example.invalid/rss", "timeoutSec": 0.1},
            },
        )

        with mock.patch.object(
            module.proactive_scanner.ProactiveScanner,
            "scan_arxiv_rss",
            return_value={
                "ok": True,
                "findings": [{"source": "arxiv_rss", "title": "Fresh paper", "link": "https://arxiv.org/abs/1234.5678"}],
                "degraded": False,
                "reason": "",
            },
        ):
            with mock.patch.object(
                module,
                "autopilot_once",
                return_value={"ok": True, "skipped": True, "reason": "scanner_test", "stepsRun": 0},
            ):
                out = module.scheduler_run_once(self._scheduler_args())

        self.assertTrue(out.get("ok"), out)
        scanner = out.get("scanner") or {}
        self.assertGreaterEqual(int(scanner.get("created") or 0), 4, out)
        snapshot = self._read_task_snapshot()
        tasks = list((snapshot.get("tasks") or {}).values())
        self.assertEqual(len(tasks), 4, snapshot)
        assignees = {str(task.get("assigneeHint") or "") for task in tasks}
        self.assertIn("debugger", assignees, tasks)
        self.assertIn("coder", assignees, tasks)
        self.assertIn("invest-analyst", assignees, tasks)
        self.assertIn("paper-ingestor", assignees, tasks)
        metrics_path = self.root / "state" / "ops.metrics.jsonl"
        metric_rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        scanner_ticks = [row for row in metric_rows if row.get("event") == "scheduler_scanner"]
        created_rows = [row for row in metric_rows if row.get("event") == "scanner_task_created"]
        self.assertEqual(len(scanner_ticks), 1, metric_rows)
        self.assertGreaterEqual(int(scanner_ticks[0].get("created") or 0), 4, scanner_ticks)
        self.assertEqual(len(created_rows), 4, metric_rows)

    def test_scheduler_run_scanner_dedupes_existing_findings_across_ticks(self):
        module = load_milestone_module()
        src = self.root / "scripts" / "demo.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("def f():\n    pass  # TODO: add retry\n", encoding="utf-8")
        (self.root / "state" / "pytest.latest.log").write_text(
            "FAILED tests/test_demo.py::test_abc - AssertionError: expected x\n",
            encoding="utf-8",
        )
        self._write_json_file(
            "state/feishu.messages.json",
            [{"text": "这个需求有变更，接口要改成批量版本"}],
        )
        self._write_json_file(
            "config/scanner-policy.json",
            {
                "enabled": True,
                "dryRun": False,
                "todoComments": {"enabled": True, "paths": ["scripts"]},
                "pytestFailures": {"enabled": True, "logPath": "state/pytest.latest.log"},
                "feishuMessages": {"enabled": True, "messagesPath": "state/feishu.messages.json"},
                "arxivRss": {"enabled": False},
            },
        )

        with mock.patch.object(
            module,
            "autopilot_once",
            return_value={"ok": True, "skipped": True, "reason": "scanner_test", "stepsRun": 0},
        ):
            first = module.scheduler_run_once(self._scheduler_args())
            second = module.scheduler_run_once(self._scheduler_args())

        self.assertTrue(first.get("ok"), first)
        self.assertEqual(int((first.get("scanner") or {}).get("created") or 0), 3, first)
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(int((second.get("scanner") or {}).get("created") or 0), 0, second)
        self.assertGreaterEqual(int((second.get("scanner") or {}).get("skipped") or 0), 3, second)
        snapshot = self._read_task_snapshot()
        self.assertEqual(len((snapshot.get("tasks") or {})), 3, snapshot)
        self.assertTrue((self.root / "state" / "scanner.registry.json").exists(), snapshot)

    def test_scheduler_run_scanner_exception_does_not_break_main_flow(self):
        module = load_milestone_module()
        self._write_json_file(
            "config/scanner-policy.json",
            {
                "enabled": True,
                "dryRun": False,
                "todoComments": {"enabled": True, "paths": ["scripts"]},
                "pytestFailures": {"enabled": False},
                "feishuMessages": {"enabled": False},
                "arxivRss": {"enabled": False},
            },
        )

        def scanner_boom(*_args, **_kwargs):
            raise RuntimeError("scanner exploded")

        with mock.patch.object(module, "run_proactive_scanner_cycle", side_effect=scanner_boom):
            with mock.patch.object(
                module,
                "autopilot_once",
                return_value={"ok": True, "skipped": True, "reason": "scanner_test", "stepsRun": 0},
            ):
                out = module.scheduler_run_once(self._scheduler_args())

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("intent"), "scheduler_run", out)
        scanner = out.get("scanner") or {}
        self.assertTrue(scanner.get("degraded"), out)
        self.assertIn("scanner exploded", str(scanner.get("reason") or ""), out)
        self.assertEqual((out.get("run") or {}).get("reason"), "scanner_test", out)

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
        body = card.get("body") if isinstance(card, dict) else []
        texts = "\n".join(str((item or {}).get("text") or "") for item in body if isinstance(item, dict))
        self.assertIn("协作线程摘要", texts, out)
        self.assertIn("专家组状态", texts, out)

    def test_build_manager_kpis_blocked_recovery_rate_uses_timestamp_order_for_out_of_order_events(self):
        now_ts = int(time.time())
        metrics_path = self.root / "state" / "ops.metrics.jsonl"
        at_blocked = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts + 40))
        at_done = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts + 50))
        rows = [
            # Intentionally out of order: done happens after blocked by timestamp.
            {"event": "dispatch_done", "taskId": "T-BR-1", "ts": now_ts + 20},
            {"event": "dispatch_blocked", "taskId": "T-BR-1", "ts": now_ts + 10},
            {"event": "dispatch_blocked", "taskId": "T-BR-2", "ts": now_ts + 30},
            # Missing ts should safely fall back to "at".
            {"event": "dispatch_done", "taskId": "T-BR-3", "at": at_done},
            {"event": "dispatch_blocked", "taskId": "T-BR-3", "at": at_blocked},
            {"event": "dispatch_done", "taskId": "T-DONE-ONLY", "ts": now_ts + 60},
        ]
        metrics_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=True) for row in rows) + "\n",
            encoding="utf-8",
        )

        module = load_milestone_module()
        kpis = module.build_manager_kpis(str(self.root), tasks={}, days=1)
        self.assertIn("blockedRecoveryRate", kpis, kpis)
        self.assertAlmostEqual(kpis.get("blockedRecoveryRate"), round(2.0 / 3.0, 4), places=4, msg=str(kpis))

    def test_feishu_router_report_failure_returns_structured_error(self):
        module = load_milestone_module()
        real_build_report = module.build_manager_report
        real_send_group_message = module.send_group_message
        out_buffer = io.StringIO()
        sent_messages = []

        def boom(*_args, **_kwargs):
            raise RuntimeError("report exploded")

        def fake_send_group_message(group_id, account_id, text, mode):
            sent_messages.append(
                {
                    "group_id": group_id,
                    "account_id": account_id,
                    "text": text,
                    "mode": mode,
                }
            )
            return {"ok": True, "dryRun": True, "payload": {"text": text}}

        args = argparse.Namespace(
            root=str(self.root),
            actor="orchestrator",
            text="@orchestrator report daily",
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
            module.build_manager_report = boom
            module.send_group_message = fake_send_group_message
            with contextlib.redirect_stdout(out_buffer):
                rc = module.cmd_feishu_router(args)
        finally:
            module.build_manager_report = real_build_report
            module.send_group_message = real_send_group_message

        self.assertEqual(rc, 1)
        payload = json.loads(out_buffer.getvalue().strip())
        self.assertFalse(payload.get("ok"), payload)
        self.assertEqual(payload.get("intent"), "report", payload)
        self.assertIn("report exploded", str(payload.get("error") or ""), payload)
        self.assertTrue((payload.get("send") or {}).get("ok"), payload)
        self.assertTrue(sent_messages, payload)
        self.assertIn("report exploded", str(sent_messages[0].get("text") or ""), sent_messages)

    def test_feishu_router_report_daily_generates_markdown_with_kpi_metadata(self):
        now_ts = int(time.time())
        metrics_path = self.root / "state" / "ops.metrics.jsonl"
        metrics_path.write_text(
            json.dumps(
                {
                    "event": "dispatch_done",
                    "at": "2026-03-01T00:00:00Z",
                    "ts": now_ts,
                    "taskId": "T-RPT-1",
                    "cycleMs": 1500,
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
            "@orchestrator report daily",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("intent"), "report", out)
        self.assertEqual(out.get("period"), "daily", out)
        report_path = Path(str(out.get("path") or ""))
        self.assertTrue(report_path.exists(), out)
        self.assertIn("state/reports", report_path.as_posix(), out)
        self.assertIn("daily", report_path.name, out)

        kpis = out.get("kpis") or {}
        self.assertIsInstance(kpis, dict, out)
        self.assertIn("taskCompletionRate", kpis, out)
        self.assertIn("blockedRecoveryRate", kpis, out)
        self.assertIn("expertGroupMedianClosureMinutes", kpis, out)

        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn("看板进度", report_text, out)
        self.assertIn("核心 KPI", report_text, out)
        self.assertIn("风险TOP", report_text, out)
        self.assertIn("专家组状态摘要", report_text, out)
        self.assertIn("下一步建议", report_text, out)

    def test_build_manager_report_surfaces_degraded_state_when_ops_metrics_aggregate_fails(self):
        module = load_milestone_module()
        real_aggregate = module.ops_metrics.aggregate_metrics

        def boom(*_args, **_kwargs):
            raise RuntimeError("aggregate unavailable")

        try:
            module.ops_metrics.aggregate_metrics = boom
            report = module.build_manager_report(str(self.root), period="daily")
        finally:
            module.ops_metrics.aggregate_metrics = real_aggregate

        self.assertTrue(report.get("ok"), report)
        self.assertTrue(report.get("degraded"), report)
        self.assertIn("aggregate unavailable", str(report.get("opsMetricsError") or ""), report)
        warnings = report.get("warnings")
        self.assertIsInstance(warnings, list, report)
        self.assertTrue(warnings, report)

        report_path = Path(str(report.get("path") or ""))
        self.assertTrue(report_path.exists(), report)
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn("degraded: true", report_text, report)
        self.assertIn("aggregate unavailable", report_text, report)

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
        manager_kpis = out.get("managerKpis") or {}
        self.assertIsInstance(manager_kpis, dict, out)
        self.assertIn("taskCompletionRate", manager_kpis, out)
        self.assertIn("blockedRecoveryRate", manager_kpis, out)
        self.assertIn("expertGroupMedianClosureMinutes", manager_kpis, out)

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

    def test_dispatch_spawn_injects_worktree_workspace_and_active_session(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-WT1: worktree integration smoke",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        real_ensure_task_worktree = module.worktree_manager.ensure_task_worktree
        worktree_path = (self.root / "task-worktrees" / "task-T-WT1").as_posix()

        def fake_ensure_task_worktree(_root, task_id, base_ref="HEAD", **_kwargs):
            self.assertEqual(task_id, "T-WT1")
            self.assertEqual(base_ref, "HEAD")
            return {
                "ok": True,
                "created": True,
                "skipped": False,
                "reason": "created",
                "path": worktree_path,
                "branch": "task/T-WT1",
                "policy": {"enabled": True, "cleanupOnDone": False},
            }

        def fake_run_dispatch_spawn(args, _task_prompt):
            self.assertEqual(str(getattr(args, "workspace", "") or ""), worktree_path)
            return {
                "ok": True,
                "skipped": False,
                "stdout": '{"status":"done","message":"done with test log"}',
                "stderr": "",
                "command": ["python3", "bridge.py"],
                "executor": "claude_cli",
                "plannedCommand": ["python3", "bridge.py", "--workspace", worktree_path],
                "spawnResult": {"status": "done", "message": "done with test log"},
                "decision": "done",
                "detail": "done with test log",
                "reasonCode": "done_with_evidence",
                "acceptanceReasonCode": "accepted",
                "normalizedReport": {
                    "status": "done",
                    "summary": "done with test log",
                    "evidence": ["test log output"],
                },
                "metrics": {"elapsedMs": 7, "tokenUsage": 11},
            }

        try:
            module.worktree_manager.ensure_task_worktree = fake_ensure_task_worktree
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            args = argparse.Namespace(
                root=self.root.as_posix(),
                task_id="T-WT1",
                agent="coder",
                task="worktree dispatch check",
                actor="orchestrator",
                session_id="",
                group_id="oc_test",
                account_id="orchestrator",
                mode="dry-run",
                timeout_sec=0,
                spawn=True,
                spawn_cmd="",
                spawn_output="",
                visibility_mode="handoff_visible",
                selection={},
            )
            out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn
            module.worktree_manager.ensure_task_worktree = real_ensure_task_worktree

        self.assertTrue(out.get("ok"), out)
        worktree = out.get("worktree") if isinstance(out.get("worktree"), dict) else {}
        self.assertEqual(worktree.get("path"), worktree_path, out)
        spawn = out.get("spawn") if isinstance(out.get("spawn"), dict) else {}
        spawn_worktree = spawn.get("worktree") if isinstance(spawn.get("worktree"), dict) else {}
        self.assertEqual(spawn_worktree.get("path"), worktree_path, out)
        active = spawn.get("activeSession") if isinstance(spawn.get("activeSession"), dict) else {}
        self.assertEqual(active.get("taskId"), "T-WT1", out)
        self.assertEqual(active.get("worktreePath"), worktree_path, out)
        self.assertEqual(active.get("status"), "done", out)

        active_state = module.session_registry.load_active_sessions(self.root.as_posix())
        active_row = ((active_state.get("sessions") or {}).get("T-WT1")) or {}
        self.assertEqual(active_row.get("worktreePath"), worktree_path, active_state)
        self.assertEqual(active_row.get("status"), "done", active_state)

    def test_dispatch_spawn_done_triggers_worktree_cleanup(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-WTC2: done cleanup",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        real_ensure_task_worktree = module.worktree_manager.ensure_task_worktree
        real_cleanup_task_worktree = module.worktree_manager.cleanup_task_worktree
        cleanup_calls = []
        worktree_path = (self.root / "task-worktrees" / "task-T-WTC2").as_posix()

        def fake_ensure_task_worktree(_root, task_id, base_ref="HEAD", **_kwargs):
            self.assertEqual(task_id, "T-WTC2")
            self.assertEqual(base_ref, "HEAD")
            return {
                "ok": True,
                "created": True,
                "skipped": False,
                "reason": "created",
                "path": worktree_path,
                "branch": "task/T-WTC2",
                "policy": {
                    "enabled": True,
                    "cleanupOnDone": True,
                    "rootDir": (self.root / "task-worktrees").as_posix(),
                    "branchPrefix": "task",
                },
            }

        def fake_cleanup_task_worktree(_root, task_id, force=False, policy_override=None, **_kwargs):
            cleanup_calls.append(
                {
                    "taskId": task_id,
                    "force": force,
                    "policy": dict(policy_override or {}),
                }
            )
            return {
                "ok": True,
                "removed": True,
                "skipped": False,
                "reason": "removed",
                "path": worktree_path,
                "branch": "task/T-WTC2",
                "policy": dict(policy_override or {}),
            }

        def fake_run_dispatch_spawn(_args, _task_prompt):
            return {
                "ok": True,
                "skipped": False,
                "stdout": '{"status":"done","message":"done with test log"}',
                "stderr": "",
                "command": ["python3", "bridge.py"],
                "executor": "claude_cli",
                "plannedCommand": ["python3", "bridge.py", "--workspace", worktree_path],
                "spawnResult": {"status": "done", "message": "done with test log"},
                "decision": "done",
                "detail": "done with test log",
                "reasonCode": "done_with_evidence",
                "acceptanceReasonCode": "accepted",
                "normalizedReport": {
                    "status": "done",
                    "summary": "done with test log",
                    "evidence": ["test log output"],
                },
                "metrics": {"elapsedMs": 7, "tokenUsage": 11},
            }

        try:
            module.worktree_manager.ensure_task_worktree = fake_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = fake_cleanup_task_worktree
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            args = argparse.Namespace(
                root=self.root.as_posix(),
                task_id="T-WTC2",
                agent="coder",
                task="done cleanup",
                actor="orchestrator",
                session_id="",
                group_id="oc_test",
                account_id="orchestrator",
                mode="dry-run",
                timeout_sec=0,
                spawn=True,
                spawn_cmd="",
                spawn_output="",
                visibility_mode="handoff_visible",
                selection={},
            )
            out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn
            module.worktree_manager.ensure_task_worktree = real_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = real_cleanup_task_worktree

        self.assertTrue(out.get("ok"), out)
        self.assertEqual(len(cleanup_calls), 1, cleanup_calls)
        self.assertEqual(cleanup_calls[0]["taskId"], "T-WTC2", cleanup_calls)
        self.assertFalse(cleanup_calls[0]["force"], cleanup_calls)
        cleanup = ((out.get("worktree") or {}).get("cleanup") or {})
        self.assertEqual(cleanup.get("reason"), "removed", out)
        spawn_cleanup = ((((out.get("spawn") or {}).get("worktree") or {}).get("cleanup")) or {})
        self.assertEqual(spawn_cleanup.get("reason"), "removed", out)

    def test_dispatch_spawn_blocked_triggers_worktree_cleanup(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-WTC3: blocked cleanup",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        real_ensure_task_worktree = module.worktree_manager.ensure_task_worktree
        real_cleanup_task_worktree = module.worktree_manager.cleanup_task_worktree
        cleanup_calls = []
        worktree_path = (self.root / "task-worktrees" / "task-T-WTC3").as_posix()

        def fake_ensure_task_worktree(_root, task_id, base_ref="HEAD", **_kwargs):
            self.assertEqual(task_id, "T-WTC3")
            self.assertEqual(base_ref, "HEAD")
            return {
                "ok": True,
                "created": True,
                "skipped": False,
                "reason": "created",
                "path": worktree_path,
                "branch": "task/T-WTC3",
                "policy": {
                    "enabled": True,
                    "cleanupOnDone": True,
                    "rootDir": (self.root / "task-worktrees").as_posix(),
                    "branchPrefix": "task",
                },
            }

        def fake_cleanup_task_worktree(_root, task_id, force=False, policy_override=None, **_kwargs):
            cleanup_calls.append(
                {
                    "taskId": task_id,
                    "force": force,
                    "policy": dict(policy_override or {}),
                }
            )
            return {
                "ok": True,
                "removed": True,
                "skipped": False,
                "reason": "removed",
                "path": worktree_path,
                "branch": "task/T-WTC3",
                "policy": dict(policy_override or {}),
            }

        def fake_run_dispatch_spawn(_args, _task_prompt):
            return {
                "ok": True,
                "skipped": False,
                "stdout": '{"status":"blocked","message":"waiting upstream"}',
                "stderr": "",
                "command": ["python3", "bridge.py"],
                "executor": "claude_cli",
                "plannedCommand": ["python3", "bridge.py", "--workspace", worktree_path],
                "spawnResult": {"status": "blocked", "message": "waiting upstream"},
                "decision": "blocked",
                "detail": "waiting upstream",
                "reasonCode": "external_dependency",
                "metrics": {"elapsedMs": 5, "tokenUsage": 3},
            }

        try:
            module.worktree_manager.ensure_task_worktree = fake_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = fake_cleanup_task_worktree
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            args = argparse.Namespace(
                root=self.root.as_posix(),
                task_id="T-WTC3",
                agent="coder",
                task="blocked cleanup",
                actor="orchestrator",
                session_id="",
                group_id="oc_test",
                account_id="orchestrator",
                mode="dry-run",
                timeout_sec=0,
                spawn=True,
                spawn_cmd="",
                spawn_output="",
                visibility_mode="handoff_visible",
                selection={},
            )
            out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn
            module.worktree_manager.ensure_task_worktree = real_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = real_cleanup_task_worktree

        self.assertTrue(out.get("ok"), out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)
        self.assertEqual(len(cleanup_calls), 1, cleanup_calls)
        cleanup = ((out.get("worktree") or {}).get("cleanup") or {})
        self.assertEqual(cleanup.get("reason"), "removed", out)

    def test_dispatch_spawn_cleanup_failure_is_metadata_only(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-WTC4: cleanup failure",
        ])

        module = load_milestone_module()
        real_run_dispatch_spawn = module.run_dispatch_spawn
        real_ensure_task_worktree = module.worktree_manager.ensure_task_worktree
        real_cleanup_task_worktree = module.worktree_manager.cleanup_task_worktree
        worktree_path = (self.root / "task-worktrees" / "task-T-WTC4").as_posix()

        def fake_ensure_task_worktree(_root, task_id, base_ref="HEAD", **_kwargs):
            self.assertEqual(task_id, "T-WTC4")
            self.assertEqual(base_ref, "HEAD")
            return {
                "ok": True,
                "created": True,
                "skipped": False,
                "reason": "created",
                "path": worktree_path,
                "branch": "task/T-WTC4",
                "policy": {
                    "enabled": True,
                    "cleanupOnDone": True,
                    "rootDir": (self.root / "task-worktrees").as_posix(),
                    "branchPrefix": "task",
                },
            }

        def fake_cleanup_task_worktree(_root, _task_id, force=False, policy_override=None, **_kwargs):
            self.assertFalse(force)
            self.assertTrue((policy_override or {}).get("cleanupOnDone"))
            return {
                "ok": False,
                "removed": False,
                "skipped": False,
                "reason": "remove_failed",
                "error": "worktree busy",
                "path": worktree_path,
                "branch": "task/T-WTC4",
                "policy": dict(policy_override or {}),
            }

        def fake_run_dispatch_spawn(_args, _task_prompt):
            return {
                "ok": True,
                "skipped": False,
                "stdout": '{"status":"done","message":"done with test log"}',
                "stderr": "",
                "command": ["python3", "bridge.py"],
                "executor": "claude_cli",
                "plannedCommand": ["python3", "bridge.py", "--workspace", worktree_path],
                "spawnResult": {"status": "done", "message": "done with test log"},
                "decision": "done",
                "detail": "done with test log",
                "reasonCode": "done_with_evidence",
                "acceptanceReasonCode": "accepted",
                "normalizedReport": {
                    "status": "done",
                    "summary": "done with test log",
                    "evidence": ["test log output"],
                },
                "metrics": {"elapsedMs": 7, "tokenUsage": 11},
            }

        try:
            module.worktree_manager.ensure_task_worktree = fake_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = fake_cleanup_task_worktree
            module.run_dispatch_spawn = fake_run_dispatch_spawn
            args = argparse.Namespace(
                root=self.root.as_posix(),
                task_id="T-WTC4",
                agent="coder",
                task="cleanup failure",
                actor="orchestrator",
                session_id="",
                group_id="oc_test",
                account_id="orchestrator",
                mode="dry-run",
                timeout_sec=0,
                spawn=True,
                spawn_cmd="",
                spawn_output="",
                visibility_mode="handoff_visible",
                selection={},
            )
            out = module.dispatch_once(args)
        finally:
            module.run_dispatch_spawn = real_run_dispatch_spawn
            module.worktree_manager.ensure_task_worktree = real_ensure_task_worktree
            module.worktree_manager.cleanup_task_worktree = real_cleanup_task_worktree

        self.assertTrue(out.get("ok"), out)
        cleanup = ((out.get("worktree") or {}).get("cleanup") or {})
        self.assertFalse(cleanup.get("ok"), out)
        self.assertEqual(cleanup.get("reason"), "remove_failed", out)
        self.assertEqual(cleanup.get("error"), "worktree busy", out)


if __name__ == "__main__":
    unittest.main()
