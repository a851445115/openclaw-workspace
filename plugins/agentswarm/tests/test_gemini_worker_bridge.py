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
from typing import Optional
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "lib" / "gemini_worker_bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("gemini_worker_bridge_for_test", str(BRIDGE))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load gemini_worker_bridge module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeminiWorkerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _invoke_main(self, task: str = "执行任务", env: Optional[dict] = None):
        argv = [
            "gemini_worker_bridge.py",
            "--root",
            str(self.root),
            "--task-id",
            "T-301",
            "--agent",
            "coder",
            "--task",
            task,
            "--workspace",
            str(self.workspace),
        ]
        merged_env = {
            "GEMINI_WORKER_FAKE_OUTPUT": "",
            "GEMINI_WORKER_MODEL": "",
        }
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

    def test_command_uses_default_model_and_prompt_flags(self):
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
            code, out = self._invoke_main(task="写一段更新说明")

        self.assertEqual(code, 0, out)
        cmd = captured.get("cmd") or []
        self.assertTrue(cmd and cmd[0] == "gemini", cmd)
        self.assertIn("--model", cmd, f"expected --model in command, got: {cmd}")
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3.1-pro")
        self.assertIn("--approval-mode", cmd, f"expected --approval-mode in command, got: {cmd}")
        self.assertEqual(cmd[cmd.index("--approval-mode") + 1], "yolo")
        self.assertIn("--sandbox", cmd, f"expected --sandbox in command, got: {cmd}")
        self.assertIn("--prompt", cmd, f"expected --prompt in command, got: {cmd}")
        prompt_value = cmd[cmd.index("--prompt") + 1]
        self.assertIn("写一段更新说明", prompt_value)
        self.assertIn("CRITICAL RULES", prompt_value, "system prompt should be prepended")
        self.assertIn("--output-format", cmd, f"expected --output-format in command, got: {cmd}")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        kwargs = captured.get("kwargs") or {}
        self.assertIs(kwargs.get("stdin"), self.bridge.subprocess.DEVNULL)
        env = kwargs.get("env") or {}
        self.assertEqual(env.get("CI"), "1")
        self.assertEqual(env.get("NO_COLOR"), "1")
        self.assertEqual(env.get("TERM"), "dumb")

    def test_model_supports_env_override(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"status":"progress","summary":"ok"}',
                stderr="",
            )

        with mock.patch.object(self.bridge.subprocess, "run", side_effect=fake_run):
            _, out = self._invoke_main(task="写一段更新说明", env={"GEMINI_WORKER_MODEL": "gemini-3-flash-preview"})

        self.assertEqual(out.get("status"), "progress", out)
        cmd = captured.get("cmd") or []
        self.assertIn("--model", cmd, cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-3-flash-preview")

    def test_noisy_stdout_is_parsed_into_report(self):
        noisy_output = (
            "MCP transport warning: {not-json}\n"
            '{"status":"done","summary":"noise parsed","evidence":["logs/gemini.log"]}'
        )

        with mock.patch.object(
            self.bridge.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=["gemini"],
                returncode=0,
                stdout=noisy_output,
                stderr="",
            ),
        ):
            _, out = self._invoke_main(task="解析带噪声输出")

        self.assertEqual(out.get("status"), "done", out)
        self.assertEqual(out.get("summary"), "noise parsed", out)
        self.assertEqual(out.get("evidence"), ["logs/gemini.log"], out)

    def test_fake_output_file_supports_structured_output_envelope(self):
        fake_file = self.root / "fake.json"
        fake_file.write_text(
            json.dumps(
                {
                    "result": '{"status":"blocked","summary":"fallback"}',
                    "structured_output": {
                        "status": "done",
                        "summary": "fake structured",
                        "evidence": ["tests/test_gemini_worker_bridge.py"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with mock.patch.object(self.bridge.subprocess, "run", side_effect=AssertionError("should not call subprocess in fake mode")):
            _, out = self._invoke_main(env={"GEMINI_WORKER_FAKE_OUTPUT": str(fake_file)})

        self.assertEqual(out.get("status"), "done", out)
        self.assertEqual(out.get("summary"), "fake structured", out)
        self.assertEqual(out.get("evidence"), ["tests/test_gemini_worker_bridge.py"], out)

    def test_build_schema_includes_checkpoint_contract(self):
        schema = self.bridge.build_schema()
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        checkpoint = props.get("checkpoint") if isinstance(props.get("checkpoint"), dict) else {}
        checkpoint_props = checkpoint.get("properties") if isinstance(checkpoint.get("properties"), dict) else {}
        required = checkpoint.get("required") if isinstance(checkpoint.get("required"), list) else []

        self.assertIn("checkpoint", props, schema)
        self.assertEqual(checkpoint_props.get("progressPercent", {}).get("type"), "integer", schema)
        self.assertEqual(checkpoint_props.get("completed", {}).get("type"), "array", schema)
        self.assertEqual(checkpoint_props.get("remaining", {}).get("type"), "array", schema)
        self.assertEqual(checkpoint_props.get("nextAction", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("continueHint", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("stallSignal", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("evidenceDelta", {}).get("type"), "array", schema)
        self.assertEqual(
            required,
            [
                "progressPercent",
                "completed",
                "remaining",
                "nextAction",
                "continueHint",
                "stallSignal",
                "evidenceDelta",
            ],
            schema,
        )

    def test_normalize_result_preserves_checkpoint_payload(self):
        result = self.bridge.normalize_result(
            "T-CP",
            "coder",
            {
                "status": "progress",
                "summary": "checkpoint update",
                "checkpoint": {
                    "progressPercent": 41,
                    "completed": ["gathered inputs"],
                    "remaining": ["draft proposal"],
                    "nextAction": "finish draft",
                    "continueHint": "continue",
                    "stallSignal": "none",
                    "evidenceDelta": ["new outline generated"],
                },
            },
        )

        checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), dict) else {}
        self.assertEqual(checkpoint.get("progressPercent"), 41, result)
        self.assertEqual(checkpoint.get("completed"), ["gathered inputs"], result)
        self.assertEqual(checkpoint.get("remaining"), ["draft proposal"], result)
        self.assertEqual(checkpoint.get("nextAction"), "finish draft", result)
        self.assertEqual(checkpoint.get("continueHint"), "continue", result)
        self.assertEqual(checkpoint.get("stallSignal"), "none", result)
        self.assertEqual(checkpoint.get("evidenceDelta"), ["new outline generated"], result)


if __name__ == "__main__":
    unittest.main()
