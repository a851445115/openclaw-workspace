import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "lib" / "claude_worker_bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("claude_worker_bridge_for_test", str(BRIDGE))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load claude_worker_bridge module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeWorkerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke_main(self, task: str = "执行任务", env: dict | None = None):
        argv = [
            "claude_worker_bridge.py",
            "--root",
            str(self.root),
            "--task-id",
            "T-101",
            "--agent",
            "coder",
            "--task",
            task,
            "--workspace",
            str(self.workspace),
        ]
        merged_env = {"CLAUDE_WORKER_FAKE_OUTPUT": ""}
        if env:
            merged_env.update(env)
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.dict(os.environ, merged_env, clear=False):
                with contextlib.redirect_stdout(buf):
                    rc = self.bridge.main()
        out_text = buf.getvalue().strip()
        self.assertTrue(out_text, "main() should print a JSON line")
        return rc, json.loads(out_text)

    def test_command_uses_prompt_flag(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"status":"progress","summary":"ok"}',
                stderr="",
            )

        with mock.patch.object(self.bridge.subprocess, "run", side_effect=fake_run):
            code, out = self._invoke_main(task="请修复 coder->claude_cli 桥接")

        self.assertEqual(code, 0, out)
        cmd = captured.get("cmd") or []
        self.assertIn("--print", cmd)
        self.assertIn("-p", cmd, f"expected explicit prompt flag in command, got: {cmd}")
        self.assertEqual(cmd[cmd.index("-p") + 1], "请修复 coder->claude_cli 桥接")
        self.assertEqual(out.get("status"), "progress")

    def test_structured_output_takes_priority_from_claude_envelope(self):
        envelope = {
            "result": '{"status":"blocked","summary":"fallback"}',
            "structured_output": {
                "status": "done",
                "summary": "structured wins",
                "evidence": ["logs/worker.log"],
            },
        }

        with mock.patch.object(
            self.bridge.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["claude"],
                returncode=0,
                stdout=json.dumps(envelope, ensure_ascii=False),
                stderr="",
            ),
        ):
            _, out = self._invoke_main(task="提取 structured_output")

        self.assertEqual(out.get("status"), "done", out)
        self.assertEqual(out.get("summary"), "structured wins", out)
        self.assertEqual(out.get("evidence"), ["logs/worker.log"], out)

    def test_fake_output_file_supports_structured_output_envelope(self):
        fake_file = self.root / "fake.json"
        fake_file.write_text(
            json.dumps(
                {
                    "result": '{"status":"blocked","summary":"fallback"}',
                    "structured_output": {
                        "status": "done",
                        "summary": "fake structured",
                        "evidence": ["tests/test_claude_worker_bridge.py"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with mock.patch.object(self.bridge.subprocess, "run", side_effect=AssertionError("should not call subprocess in fake mode")):
            _, out = self._invoke_main(env={"CLAUDE_WORKER_FAKE_OUTPUT": str(fake_file)})

        self.assertEqual(out.get("status"), "done", out)
        self.assertEqual(out.get("summary"), "fake structured", out)
        self.assertEqual(out.get("evidence"), ["tests/test_claude_worker_bridge.py"], out)


if __name__ == "__main__":
    unittest.main()
