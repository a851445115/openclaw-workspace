import json
import importlib.util
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SESSION_REGISTRY = REPO / "scripts" / "lib" / "session_registry.py"


def load_session_registry_module():
    spec = importlib.util.spec_from_file_location("session_registry_module_for_test", str(SESSION_REGISTRY))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load session_registry module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_session_registry_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ensure_session_creates_and_reuses_same_record(self):
        first = self.mod.ensure_session(self.root.as_posix(), "T-S001", "coder", "claude_cli")
        second = self.mod.ensure_session(self.root.as_posix(), "T-S001", "coder", "claude_cli")

        self.assertTrue(first.get("created"), first)
        self.assertFalse(second.get("created"), second)
        self.assertEqual(
            (first.get("session") or {}).get("sessionId"),
            (second.get("session") or {}).get("sessionId"),
            (first, second),
        )
        self.assertEqual((second.get("session") or {}).get("retryCount"), 0, second)
        self.assertEqual((second.get("session") or {}).get("status"), "active", second)

    def test_record_attempt_and_status_transitions(self):
        ensured = self.mod.ensure_session(self.root.as_posix(), "T-S002", "debugger", "codex_cli")
        session_id = (ensured.get("session") or {}).get("sessionId")

        attempted = self.mod.record_attempt(
            self.root.as_posix(),
            "T-S002",
            "debugger",
            "codex_cli",
            reason_code="spawn_failed",
            detail="worker crashed",
        )
        failed = self.mod.mark_failed(
            self.root.as_posix(),
            "T-S002",
            "debugger",
            "codex_cli",
            reason_code="spawn_failed",
            detail="worker crashed",
        )
        done = self.mod.mark_done(self.root.as_posix(), "T-S002", "debugger", "codex_cli")
        metadata = self.mod.build_session_metadata(done)

        self.assertEqual((attempted.get("session") or {}).get("retryCount"), 1, attempted)
        self.assertEqual((failed.get("session") or {}).get("status"), "failed", failed)
        self.assertEqual((done.get("session") or {}).get("status"), "done", done)
        self.assertEqual(metadata.get("sessionId"), session_id, metadata)
        self.assertEqual(metadata.get("retryCount"), 1, metadata)

    def test_save_registry_uses_atomic_replace_in_same_directory(self):
        target_path = self.root / "state" / "worker-sessions.json"

        with mock.patch.object(self.mod.os, "replace", wraps=self.mod.os.replace) as mocked_replace:
            self.mod.ensure_session(self.root.as_posix(), "T-S003", "coder", "claude_cli")

        self.assertGreaterEqual(mocked_replace.call_count, 1, mocked_replace.call_args_list)
        for call in mocked_replace.call_args_list:
            src, dst = call.args[:2]
            self.assertEqual(Path(dst), target_path, call.args)
            self.assertEqual(Path(src).parent, Path(dst).parent, call.args)

    def test_record_attempt_is_thread_safe(self):
        workers = 12
        attempts_per_worker = 8
        barrier = threading.Barrier(workers)
        errors = []

        self.mod.ensure_session(self.root.as_posix(), "T-S004", "coder", "claude_cli")

        def _worker(_idx: int) -> None:
            try:
                barrier.wait(timeout=3)
                for _ in range(attempts_per_worker):
                    self.mod.record_attempt(
                        self.root.as_posix(),
                        "T-S004",
                        "coder",
                        "claude_cli",
                        reason_code="spawn_failed",
                        detail="parallel-run",
                    )
            except Exception as err:  # pragma: no cover - assertion handles this
                errors.append(err)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_worker, range(workers)))

        self.assertFalse(errors, errors)
        state = self.mod.load_registry(self.root.as_posix())
        key = self.mod.session_key("T-S004", "coder", "claude_cli")
        entry = ((state.get("sessions") or {}).get(key) or {})
        self.assertEqual(entry.get("retryCount"), workers * attempts_per_worker, state)

    def test_active_session_upsert_heartbeat_and_mark_status(self):
        upserted = self.mod.upsert_active_session(
            self.root.as_posix(),
            "T-ACT-001",
            worktree_path="/tmp/task-T-ACT-001",
            pid=12345,
            tmux_session="agent-T-ACT-001",
            status="running",
        )
        active = upserted.get("activeSession") or {}
        self.assertEqual(active.get("taskId"), "T-ACT-001", upserted)
        self.assertEqual(active.get("worktreePath"), "/tmp/task-T-ACT-001", upserted)
        self.assertEqual(active.get("pid"), 12345, upserted)
        self.assertEqual(active.get("tmuxSession"), "agent-T-ACT-001", upserted)
        self.assertEqual(active.get("status"), "running", upserted)
        self.assertTrue(str(active.get("startTime") or "").strip(), upserted)
        self.assertTrue(str(active.get("lastHeartbeat") or "").strip(), upserted)

        heartbeat = self.mod.heartbeat_active_session(
            self.root.as_posix(),
            "T-ACT-001",
            pid=22222,
            tmux_session="agent-T-ACT-001",
        )
        self.assertEqual((heartbeat.get("activeSession") or {}).get("pid"), 22222, heartbeat)
        self.assertEqual((heartbeat.get("activeSession") or {}).get("status"), "running", heartbeat)

        marked = self.mod.mark_active_session_status(self.root.as_posix(), "T-ACT-001", status="done")
        self.assertEqual((marked.get("activeSession") or {}).get("status"), "done", marked)

        loaded = self.mod.load_active_sessions(self.root.as_posix())
        final_row = ((loaded.get("sessions") or {}).get("T-ACT-001")) or {}
        self.assertEqual(final_row.get("status"), "done", loaded)
        self.assertEqual(final_row.get("pid"), 22222, loaded)
        self.assertEqual(final_row.get("worktreePath"), "/tmp/task-T-ACT-001", loaded)

    def test_load_active_sessions_returns_empty_default(self):
        loaded = self.mod.load_active_sessions(self.root.as_posix())
        self.assertEqual(loaded.get("sessions"), {}, loaded)
        self.assertEqual(loaded.get("updatedAt"), "", loaded)

    def test_load_active_session_policy_defaults_and_override(self):
        default_policy = self.mod.load_active_session_policy(self.root.as_posix())
        self.assertEqual(default_policy.get("heartbeatTimeoutSec"), 300, default_policy)
        self.assertEqual(default_policy.get("stalePidStatus"), "blocked", default_policy)
        self.assertEqual(default_policy.get("heartbeatTimeoutStatus"), "blocked", default_policy)

        policy_path = self.root / "config" / "active-session-policy.json"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            json.dumps(
                {
                    "heartbeatTimeoutSec": 45,
                    "stalePidStatus": "failed",
                    "heartbeatTimeoutStatus": "failed",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        overridden = self.mod.load_active_session_policy(self.root.as_posix())
        self.assertEqual(overridden.get("heartbeatTimeoutSec"), 45, overridden)
        self.assertEqual(overridden.get("stalePidStatus"), "failed", overridden)
        self.assertEqual(overridden.get("heartbeatTimeoutStatus"), "failed", overridden)

    def test_watchdog_reclaims_stale_pid_running_session(self):
        self.mod.upsert_active_session(
            self.root.as_posix(),
            "T-ACT-STALE",
            worktree_path="/tmp/task-T-ACT-STALE",
            pid=321321,
            tmux_session="agent-T-ACT-STALE",
            status="running",
        )

        with mock.patch.object(self.mod, "_pid_exists", return_value=False):
            result = self.mod.run_active_session_watchdog(self.root.as_posix(), now_ts=1_700_000_123)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("updated"), 1, result)
        self.assertEqual(result.get("stalePid"), 1, result)
        self.assertEqual(result.get("heartbeatTimeout"), 0, result)
        events = result.get("events") or []
        self.assertEqual(len(events), 1, result)
        self.assertEqual(events[0].get("taskId"), "T-ACT-STALE", result)
        self.assertEqual(events[0].get("reason"), "stale_pid", result)
        self.assertEqual(events[0].get("status"), "blocked", result)

        loaded = self.mod.load_active_sessions(self.root.as_posix())
        row = ((loaded.get("sessions") or {}).get("T-ACT-STALE")) or {}
        self.assertEqual(row.get("status"), "blocked", loaded)
        self.assertEqual(row.get("stopReason"), "stale_pid", loaded)
        self.assertIn("321321", str(row.get("stopDetail") or ""), loaded)
        self.assertTrue(str(row.get("endedAt") or "").strip(), loaded)
        self.assertTrue(str(row.get("watchdogAt") or "").strip(), loaded)

    def test_watchdog_reclaims_heartbeat_timeout_running_session(self):
        self.mod.upsert_active_session(
            self.root.as_posix(),
            "T-ACT-TIMEOUT",
            worktree_path="/tmp/task-T-ACT-TIMEOUT",
            pid=0,
            tmux_session="",
            status="running",
        )
        state = self.mod.load_active_sessions(self.root.as_posix())
        sessions = state.get("sessions") or {}
        row = dict((sessions.get("T-ACT-TIMEOUT") or {}))
        row["lastHeartbeat"] = self.mod.ts_to_iso(1_700_000_000)
        sessions["T-ACT-TIMEOUT"] = row
        self.mod.save_active_sessions(self.root.as_posix(), state)

        result = self.mod.run_active_session_watchdog(self.root.as_posix(), now_ts=1_700_000_301)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("updated"), 1, result)
        self.assertEqual(result.get("stalePid"), 0, result)
        self.assertEqual(result.get("heartbeatTimeout"), 1, result)
        events = result.get("events") or []
        self.assertEqual(len(events), 1, result)
        self.assertEqual(events[0].get("taskId"), "T-ACT-TIMEOUT", result)
        self.assertEqual(events[0].get("reason"), "heartbeat_timeout", result)
        self.assertEqual(events[0].get("status"), "blocked", result)
        self.assertEqual(events[0].get("heartbeatAgeSec"), 301, result)

        loaded = self.mod.load_active_sessions(self.root.as_posix())
        final_row = ((loaded.get("sessions") or {}).get("T-ACT-TIMEOUT")) or {}
        self.assertEqual(final_row.get("status"), "blocked", loaded)
        self.assertEqual(final_row.get("stopReason"), "heartbeat_timeout", loaded)
        self.assertIn("300", str(final_row.get("stopDetail") or ""), loaded)


if __name__ == "__main__":
    unittest.main()
