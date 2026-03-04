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


if __name__ == "__main__":
    unittest.main()
