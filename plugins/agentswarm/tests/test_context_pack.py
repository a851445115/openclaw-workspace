import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONTEXT_PACK = REPO / "scripts" / "lib" / "context_pack.py"


def load_context_pack_module():
    spec = importlib.util.spec_from_file_location("context_pack_module_for_test", str(CONTEXT_PACK))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load context_pack module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ContextPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_context_pack_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_failure_builds_retry_context_pack(self):
        self.mod.record_failure(
            self.root.as_posix(),
            task_id="T-C001",
            agent="coder",
            executor="claude_cli",
            prompt_text="Run pytest and include hard evidence",
            output_text="blocked by timeout, see logs/run.log",
            blocked_reason="spawn_failed",
            artifact_index=["logs/run.log", "artifacts/report.md"],
            unfinished_checklist=["rerun pytest", "collect stack trace"],
            decision="blocked",
        )

        pack = self.mod.build_retry_context(self.root.as_posix(), "T-C001")
        self.assertEqual(pack.get("blockedReason"), "spawn_failed", pack)
        self.assertTrue(pack.get("lastPromptDigest"), pack)
        self.assertTrue(pack.get("lastOutputDigest"), pack)
        self.assertIn("logs/run.log", pack.get("artifactIndex") or [], pack)
        self.assertIn("rerun pytest", pack.get("unfinishedChecklist") or [], pack)
        self.assertTrue(pack.get("recentDecisions"), pack)

    def test_clear_task_removes_context(self):
        self.mod.record_failure(
            self.root.as_posix(),
            task_id="T-C002",
            agent="debugger",
            executor="codex_cli",
            prompt_text="Find root cause",
            output_text="blocked: no logs",
            blocked_reason="incomplete_output",
        )
        before = self.mod.build_retry_context(self.root.as_posix(), "T-C002")
        self.mod.clear_task(self.root.as_posix(), "T-C002")
        after = self.mod.build_retry_context(self.root.as_posix(), "T-C002")

        self.assertTrue(before, before)
        self.assertEqual(after, {}, after)


if __name__ == "__main__":
    unittest.main()
