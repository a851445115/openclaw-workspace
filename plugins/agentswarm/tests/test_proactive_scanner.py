import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "lib" / "proactive_scanner.py"


def load_module():
    spec = importlib.util.spec_from_file_location("proactive_scanner_module_for_tests", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load proactive_scanner module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProactiveScannerTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.scanner = self.mod.ProactiveScanner()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_todo_comments_collects_todo_and_fixme(self):
        src = self.root / "demo.py"
        src.write_text(
            "\n".join(
                [
                    "def f():",
                    "    pass  # TODO: add retry",
                    "# FIXME handle edge case",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        out = self.scanner.scan_todo_comments([str(self.root)])
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("degraded"), out)
        tags = [str(item.get("tag")) for item in out.get("findings") or []]
        self.assertIn("TODO", tags, out)
        self.assertIn("FIXME", tags, out)

    def test_scan_pytest_failures_extracts_failed_nodeids(self):
        payload = "\n".join(
            [
                "=========================== FAILURES ===========================",
                "FAILED tests/test_demo.py::test_abc - AssertionError: expected x",
                "FAILED tests/test_more.py::test_xyz - ValueError: bad value",
            ]
        )
        out = self.scanner.scan_pytest_failures(payload)
        self.assertTrue(out.get("ok"), out)
        nodeids = [str(item.get("nodeid")) for item in out.get("findings") or []]
        self.assertIn("tests/test_demo.py::test_abc", nodeids, out)
        self.assertIn("tests/test_more.py::test_xyz", nodeids, out)

    def test_scan_feishu_messages_detects_progress_and_requirement_change(self):
        messages = [
            {"text": "这个需求有变更，接口要改成批量版本"},
            "这块请尽快推进，今天给我进度",
        ]
        out = self.scanner.scan_feishu_messages(messages)
        self.assertTrue(out.get("ok"), out)
        signals = [str(item.get("signal")) for item in out.get("findings") or []]
        self.assertIn("requirement_change", signals, out)
        self.assertIn("progress_push", signals, out)

    def test_scan_arxiv_rss_network_failure_returns_degraded(self):
        out = self.scanner.scan_arxiv_rss(feed_url="http://127.0.0.1:1", timeout_sec=0.1)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("degraded"), out)
        self.assertTrue(str(out.get("reason") or "").strip(), out)


if __name__ == "__main__":
    unittest.main()
