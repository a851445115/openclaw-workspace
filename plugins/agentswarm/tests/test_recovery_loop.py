import json
import multiprocessing
import subprocess
import tempfile
import time
import unittest
import importlib.util
from unittest import mock
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
RECOVERY = SCRIPTS / "lib" / "recovery_loop.py"
INIT = SCRIPTS / "init-task-board"


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


def load_recovery_module():
    spec = importlib.util.spec_from_file_location("recovery_loop_module_for_test", str(RECOVERY))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load recovery_loop module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decide_recovery_worker(root: str, worker_index: int, rounds: int, start_event, result_queue) -> None:
    try:
        recovery_mod = load_recovery_module()
        if not start_event.wait(timeout=10):
            raise TimeoutError("start_event timed out")
        for round_index in range(rounds):
            task_id = f"T-MP-{worker_index:02d}-{round_index:02d}"
            result = recovery_mod.decide_recovery(
                root,
                task_id,
                "coder",
                "spawn_failed",
                now_ts=1_700_000_000 + round_index,
            )
            if int(result.get("attempt") or 0) != 1:
                raise AssertionError(f"unexpected attempt in worker={worker_index}: {result}")
        result_queue.put({"ok": True, "worker": worker_index})
    except Exception as err:
        result_queue.put({"ok": False, "worker": worker_index, "error": repr(err)})


def _decide_recovery_same_key_worker(root: str, rounds: int, start_event, result_queue) -> None:
    try:
        recovery_mod = load_recovery_module()
        if not start_event.wait(timeout=10):
            raise TimeoutError("start_event timed out")
        attempts = []
        for round_index in range(rounds):
            result = recovery_mod.decide_recovery(
                root,
                "T-MP-SAME",
                "coder",
                "spawn_failed",
                now_ts=1_700_100_000 + round_index,
            )
            attempt = int(result.get("attempt") or 0)
            if attempt <= 0:
                raise AssertionError(f"invalid attempt in same-key worker: {result}")
            attempts.append(attempt)
        result_queue.put({"ok": True, "attempts": attempts})
    except Exception as err:
        result_queue.put({"ok": False, "error": repr(err)})


def _hold_recovery_lock_worker(root: str, hold_seconds: float, result_queue) -> None:
    try:
        recovery_mod = load_recovery_module()
        if recovery_mod.fcntl is None:
            result_queue.put({"ok": False, "error": "fcntl unavailable"})
            return
        lock_path = Path(recovery_mod.recovery_state_lock_path(root))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fp:
            recovery_mod.fcntl.flock(lock_fp.fileno(), recovery_mod.fcntl.LOCK_EX)
            result_queue.put({"ok": True})
            time.sleep(max(0.0, hold_seconds))
    except Exception as err:
        result_queue.put({"ok": False, "error": repr(err)})


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)
        self.recovery_mod = load_recovery_module()

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

    def _status(self, task_id: str):
        return run_json([
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

    def test_no_completion_signal_enters_recovery_chain(self):
        self._create_task("T-104", "coder", "no completion signal branch")
        out = self._dispatch("T-104", "coder", '{"status":"progress","summary":"still running"}')

        self.assertEqual(out["spawn"]["reasonCode"], "no_completion_signal", out)
        self.assertEqual(out["spawn"]["action"], "retry", out)
        self.assertEqual(out["spawn"]["attempt"], 1, out)
        self.assertEqual(out["spawn"]["nextAssignee"], "debugger", out)

    def test_cooldown_prevents_attempt_increment(self):
        self._create_task("T-110", "coder", "cooldown branch")
        first = self._dispatch("T-110", "coder", '{"status":"failed","message":"first fail"}')
        second = self._dispatch("T-110", "coder", '{"status":"failed","message":"second fail"}')

        self.assertEqual(first["spawn"]["attempt"], 1, first)
        self.assertEqual(second["spawn"]["attempt"], 1, second)
        self.assertTrue(second["spawn"]["cooldownActive"], second)
        self.assertEqual(second["spawn"]["action"], "retry", second)
        self.assertEqual(second["spawn"]["nextAssignee"], "debugger", second)
        self.assertTrue(second["spawn"]["spawnSkipped"], second)
        self.assertEqual(second["spawn"]["reason"], "cooldown_active", second)
        self.assertNotIn("spawnResult", second["spawn"], second)
        self.assertTrue(second["claimSend"]["skipped"], second)
        self.assertEqual(second["claimSend"]["reason"], "cooldown_active", second)
        self.assertTrue(second["taskSend"]["skipped"], second)
        self.assertEqual(second["taskSend"]["reason"], "cooldown_active", second)

    def test_cooldown_skips_second_autopilot_spawn(self):
        self._create_task("T-111", "coder", "autopilot cooldown branch")
        first = run_json([
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
            '{"status":"failed","message":"first fail"}',
        ])
        second = run_json([
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
            '{"status":"failed","message":"second fail"}',
        ])

        self.assertEqual(first["stepsRun"], 1, first)
        self.assertEqual(second["stepsRun"], 1, second)
        second_spawn = ((second.get("steps") or [{}])[0].get("dispatch") or {}).get("spawn") or {}
        second_dispatch = (second.get("steps") or [{}])[0].get("dispatch") or {}
        self.assertTrue(second_spawn.get("spawnSkipped"), second)
        self.assertEqual(second_spawn.get("reason"), "cooldown_active", second)
        self.assertTrue(second_spawn.get("cooldownActive"), second)
        self.assertNotIn("spawnResult", second_spawn, second)
        self.assertTrue((second_dispatch.get("claimSend") or {}).get("skipped"), second)
        self.assertEqual((second_dispatch.get("claimSend") or {}).get("reason"), "cooldown_active", second)
        self.assertTrue((second_dispatch.get("taskSend") or {}).get("skipped"), second)
        self.assertEqual((second_dispatch.get("taskSend") or {}).get("reason"), "cooldown_active", second)

    def test_cooldown_isolated_by_reason_code(self):
        self._create_task("T-112", "coder", "reason-isolation branch")
        first = self._dispatch("T-112", "coder", '{"status":"failed","message":"first spawn failure"}')
        second = self._dispatch("T-112", "coder", '{"status":"done","summary":"done without evidence"}')

        self.assertEqual(first["spawn"]["reasonCode"], "spawn_failed", first)
        self.assertEqual(first["spawn"]["attempt"], 1, first)
        self.assertEqual(second["spawn"]["reasonCode"], "incomplete_output", second)
        self.assertFalse(second["spawn"].get("spawnSkipped"), second)
        self.assertEqual(second["spawn"]["attempt"], 1, second)
        self.assertTrue(second["claimSend"]["ok"], second)
        self.assertFalse(second["claimSend"].get("skipped", False), second)
        self.assertTrue(second["taskSend"]["ok"], second)
        self.assertFalse(second["taskSend"].get("skipped", False), second)

    def test_incomplete_output_retry_claims_next_assignee_to_keep_task_runnable(self):
        self._create_task("T-113", "coder", "retry should remain runnable")
        out = self._dispatch("T-113", "coder", '{"status":"done","summary":"done without evidence"}')

        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["action"], "retry", out)
        self.assertEqual(out["spawn"]["nextAssignee"], "debugger", out)
        self.assertEqual((out.get("closeApply") or {}).get("intent"), "claim_task", out)

        status = self._status("T-113")
        task = status.get("task") or {}
        self.assertEqual(task.get("status"), "in_progress", status)
        self.assertEqual(task.get("owner"), "debugger", status)

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
                        "no_completion_signal": {"maxAttempts": 1, "cooldownSec": 0},
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

    def test_current_assignee_not_in_chain_uses_chain_head(self):
        self._create_task("T-140", "qa", "fallback chain head branch")
        out = self._dispatch("T-140", "qa", '{"status":"failed","message":"qa fail"}')
        self.assertEqual(out["spawn"]["reasonCode"], "spawn_failed", out)
        self.assertEqual(out["spawn"]["action"], "retry", out)
        self.assertEqual(out["spawn"]["nextAssignee"], "coder", out)

    def test_incomplete_output_rotates_to_chain_head_instead_of_human(self):
        self._create_task("T-141", "invest-analyst", "tail role incomplete output")
        out = self._dispatch("T-141", "invest-analyst", '{"status":"done","summary":"缺少验收关键词"}')
        self.assertEqual(out["spawn"]["reasonCode"], "incomplete_output", out)
        self.assertEqual(out["spawn"]["action"], "retry", out)
        self.assertEqual(out["spawn"]["nextAssignee"], "coder", out)

    def test_clear_task_removes_only_target_entries(self):
        now_ts = int(time.time())
        self.recovery_mod.decide_recovery(self.root.as_posix(), "T-150", "coder", "spawn_failed", now_ts=now_ts)
        self.recovery_mod.decide_recovery(self.root.as_posix(), "T-151", "coder", "spawn_failed", now_ts=now_ts)

        before_first = self.recovery_mod.get_active_cooldown(self.root.as_posix(), "T-150", now_ts=now_ts)
        before_second = self.recovery_mod.get_active_cooldown(self.root.as_posix(), "T-151", now_ts=now_ts)
        self.assertTrue(before_first, before_first)
        self.assertTrue(before_second, before_second)

        cleared = self.recovery_mod.clear_task(self.root.as_posix(), "T-150")
        self.assertTrue(cleared.get("cleared"), cleared)
        self.assertEqual(cleared.get("taskId"), "T-150", cleared)

        after_first = self.recovery_mod.get_active_cooldown(self.root.as_posix(), "T-150", now_ts=now_ts)
        after_second = self.recovery_mod.get_active_cooldown(self.root.as_posix(), "T-151", now_ts=now_ts)
        self.assertEqual(after_first, {}, after_first)
        self.assertTrue(after_second, after_second)

    def test_decide_recovery_fails_closed_on_load_error_without_overwriting(self):
        state_path = self.root / "state" / "recovery.state.json"
        broken_payload = '{"entries": {"T-OLD|spawn_failed": {"taskId": "T-OLD"}'
        state_path.write_text(broken_payload, encoding="utf-8")

        expected_error = getattr(self.recovery_mod, "RecoveryStateLoadError", RuntimeError)
        with self.assertRaises(expected_error):
            self.recovery_mod.decide_recovery(
                self.root.as_posix(),
                "T-LOAD-FAIL",
                "coder",
                "spawn_failed",
                now_ts=1_700_200_000,
            )
        self.assertEqual(state_path.read_text(encoding="utf-8"), broken_payload)

    def test_clear_task_fails_closed_on_load_error(self):
        state_path = self.root / "state" / "recovery.state.json"
        broken_payload = '{"entries": '
        state_path.write_text(broken_payload, encoding="utf-8")

        expected_error = getattr(self.recovery_mod, "RecoveryStateLoadError", RuntimeError)
        with self.assertRaises(expected_error):
            self.recovery_mod.clear_task(self.root.as_posix(), "T-LOAD-FAIL")
        self.assertEqual(state_path.read_text(encoding="utf-8"), broken_payload)

    def test_save_recovery_state_fails_closed_on_load_error(self):
        state_path = self.root / "state" / "recovery.state.json"
        broken_payload = '{"entries": '
        state_path.write_text(broken_payload, encoding="utf-8")

        expected_error = getattr(self.recovery_mod, "RecoveryStateLoadError", RuntimeError)
        with self.assertRaises(expected_error):
            self.recovery_mod.save_recovery_state(
                self.root.as_posix(),
                {"entries": {"T-SAVE|spawn_failed": {"taskId": "T-SAVE"}}},
            )
        self.assertEqual(state_path.read_text(encoding="utf-8"), broken_payload)

    def test_decide_recovery_fcntl_unavailable_fails_closed_even_when_not_strict(self):
        lock_error = getattr(self.recovery_mod, "RecoveryStateLockError", RuntimeError)
        with mock.patch.object(self.recovery_mod, "fcntl", None):
            with self.assertLogs(self.recovery_mod.LOGGER.name, level="ERROR") as logs:
                with mock.patch.dict("os.environ", {"STRICT_FILE_LOCK": "false"}, clear=False):
                    with self.assertRaises(lock_error):
                        self.recovery_mod.decide_recovery(
                            self.root.as_posix(),
                            "T-LOCK-FALLBACK",
                            "coder",
                            "spawn_failed",
                            now_ts=1_700_200_100,
                        )
        combined_logs = "\n".join(logs.output)
        self.assertIn("failed to acquire recovery state lock", combined_logs)
        self.assertIn("fcntl unavailable", combined_logs)

    def test_decide_recovery_requires_file_lock_when_strict_enabled(self):
        lock_error = getattr(self.recovery_mod, "RecoveryStateLockError", RuntimeError)
        with mock.patch.object(self.recovery_mod, "fcntl", None):
            with self.assertLogs(self.recovery_mod.LOGGER.name, level="ERROR") as logs:
                with mock.patch.dict("os.environ", {"STRICT_FILE_LOCK": "true"}, clear=False):
                    with self.assertRaises(lock_error):
                        self.recovery_mod.decide_recovery(
                            self.root.as_posix(),
                            "T-LOCK-FAIL",
                            "coder",
                            "spawn_failed",
                            now_ts=1_700_200_000,
                        )
        combined_logs = "\n".join(logs.output)
        self.assertIn("failed to acquire recovery state lock", combined_logs)
        self.assertIn("STRICT_FILE_LOCK", combined_logs)

    def test_decide_recovery_lock_timeout_raises_with_wait_details(self):
        if self.recovery_mod.fcntl is None:
            self.skipTest("fcntl unavailable")

        lock_error = getattr(self.recovery_mod, "RecoveryStateLockError", RuntimeError)
        ctx = multiprocessing.get_context("spawn")
        result_queue = ctx.Queue()
        holder = ctx.Process(
            target=_hold_recovery_lock_worker,
            args=(self.root.as_posix(), 1.0, result_queue),
        )
        holder.start()
        try:
            ready = result_queue.get(timeout=5)
            self.assertTrue(ready.get("ok"), ready)
            with mock.patch.dict(
                "os.environ",
                {
                    "RECOVERY_STATE_LOCK_TIMEOUT_SEC": "0.2",
                    "RECOVERY_STATE_LOCK_RETRY_SEC": "0.05",
                    "STRICT_FILE_LOCK": "true",
                },
                clear=False,
            ):
                with self.assertLogs(self.recovery_mod.LOGGER.name, level="ERROR") as logs:
                    with self.assertRaises(lock_error):
                        self.recovery_mod.decide_recovery(
                            self.root.as_posix(),
                            "T-LOCK-TIMEOUT",
                            "coder",
                            "spawn_failed",
                            now_ts=1_700_200_200,
                        )
            combined_logs = "\n".join(logs.output)
            self.assertIn("timed out waiting", combined_logs)
            self.assertIn("recovery.state.lock", combined_logs)
        finally:
            holder.join(timeout=5)
            if holder.is_alive():
                holder.terminate()
                holder.join(timeout=5)

    def test_decide_recovery_multi_process_keeps_state_json_intact(self):
        workers = 8
        rounds = 6
        expected_keys = {
            f"T-MP-{worker_index:02d}-{round_index:02d}|spawn_failed"
            for worker_index in range(workers)
            for round_index in range(rounds)
        }

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = []
        for worker_index in range(workers):
            process = ctx.Process(
                target=_decide_recovery_worker,
                args=(self.root.as_posix(), worker_index, rounds, start_event, result_queue),
            )
            process.start()
            processes.append(process)

        start_event.set()
        for process in processes:
            process.join(timeout=60)
            self.assertFalse(process.is_alive(), f"worker did not finish: pid={process.pid}")
            self.assertEqual(process.exitcode, 0, f"worker exit code mismatch: pid={process.pid}")

        worker_results = [result_queue.get(timeout=5) for _ in range(workers)]
        failures = [item for item in worker_results if not item.get("ok")]
        self.assertEqual(failures, [], worker_results)

        state_path = self.root / "state" / "recovery.state.json"
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        entries = loaded.get("entries") if isinstance(loaded.get("entries"), dict) else {}
        self.assertEqual(set(entries.keys()), expected_keys, loaded)

    def test_decide_recovery_multi_process_same_key_counts_all_updates(self):
        workers = 6
        rounds = 5
        expected_attempts = workers * rounds

        policy_path = self.root / "config" / "recovery-policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            json.dumps(
                {
                    "default": {"maxAttempts": expected_attempts + 5, "cooldownSec": 0},
                    "reasonPolicies": {
                        "spawn_failed": {"maxAttempts": expected_attempts + 5, "cooldownSec": 0},
                    },
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = []
        for _ in range(workers):
            process = ctx.Process(
                target=_decide_recovery_same_key_worker,
                args=(self.root.as_posix(), rounds, start_event, result_queue),
            )
            process.start()
            processes.append(process)

        start_event.set()
        for process in processes:
            process.join(timeout=60)
            self.assertFalse(process.is_alive(), f"same-key worker did not finish: pid={process.pid}")
            self.assertEqual(process.exitcode, 0, f"same-key worker exit code mismatch: pid={process.pid}")

        worker_results = [result_queue.get(timeout=5) for _ in range(workers)]
        failures = [item for item in worker_results if not item.get("ok")]
        self.assertEqual(failures, [], worker_results)

        attempts = [attempt for item in worker_results for attempt in item.get("attempts", [])]
        self.assertEqual(len(attempts), expected_attempts, worker_results)
        self.assertEqual(sorted(attempts), list(range(1, expected_attempts + 1)), worker_results)

        state_path = self.root / "state" / "recovery.state.json"
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        entries = loaded.get("entries") if isinstance(loaded.get("entries"), dict) else {}
        row = entries.get("T-MP-SAME|spawn_failed") if isinstance(entries.get("T-MP-SAME|spawn_failed"), dict) else {}
        self.assertEqual(int(row.get("attempt") or 0), expected_attempts, loaded)
        self.assertEqual(row.get("action"), "retry", loaded)
        self.assertEqual(row.get("recoveryState"), "recovery_scheduled", loaded)


if __name__ == "__main__":
    unittest.main()
