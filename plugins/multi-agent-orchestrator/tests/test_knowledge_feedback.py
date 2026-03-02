import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MILE = REPO / "scripts" / "lib" / "milestones.py"
ADAPTER = REPO / "scripts" / "lib" / "knowledge_adapter.py"
BOARD = REPO / "scripts" / "lib" / "task_board.py"
INIT = REPO / "scripts" / "init-task-board"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_json(cmd):
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class KnowledgeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _create_task(self, task_id: str, assignee: str, title: str):
        _run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                f"@{assignee} create task {task_id}: {title}",
            ]
        )

    def _dispatch_once(self, task_id: str, agent: str):
        milestones = _load_module(MILE, "milestones_module_for_knowledge_feedback_test")
        args = milestones.build_parser().parse_args(
            [
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                task_id,
                "--agent",
                agent,
                "--mode",
                "dry-run",
                "--no-spawn",
            ]
        )
        return milestones.dispatch_once(args), milestones

    def _write_feedback_config(self, payload):
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "knowledge-feedback.json").write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_adapter_exception_does_not_block_dispatch(self):
        self._create_task("T-701", "coder", "知识适配器异常降级")
        out, milestones = self._dispatch_once("T-701", "coder")
        with mock.patch.object(milestones.knowledge_adapter, "fetch_feedback", side_effect=RuntimeError("adapter boom")):
            out, _ = self._dispatch_once("T-701", "coder")
        self.assertTrue(out.get("ok"), out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertTrue(knowledge.get("degraded"), out)
        self.assertIn("adapter boom", str(knowledge.get("degradeReason") or ""), out)

    def test_successful_feedback_injects_prompt_hints(self):
        self._create_task("T-702", "coder", "知识提示注入")
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": True,
                "timeoutMs": 200,
                "maxItems": 3,
                "sourceCandidates": ["state/knowledge-feedback.json"],
            }
        )
        (state_dir / "knowledge-feedback.json").write_text(
            json.dumps(
                {
                    "lessons": ["先补最小测试再改实现"],
                    "mistakes": ["避免只给阶段性汇报"],
                    "patterns": ["先验证失败再修复再复测"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        out, _ = self._dispatch_once("T-702", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertIn("KNOWLEDGE_HINTS", prompt, out)
        self.assertIn("先补最小测试再改实现", prompt, out)
        self.assertIn("避免只给阶段性汇报", prompt, out)

    def test_disabled_or_empty_feedback_falls_back_cleanly(self):
        self._create_task("T-703", "coder", "禁用知识提示回退")
        self._write_feedback_config(
            {
                "enabled": False,
                "readOnly": True,
                "timeoutMs": 200,
                "maxItems": 3,
                "sourceCandidates": ["state/not-exist.json"],
            }
        )

        out, _ = self._dispatch_once("T-703", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertNotIn("KNOWLEDGE_HINTS", prompt, out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertFalse(knowledge.get("degraded"), out)
        self.assertEqual(knowledge.get("knowledgeTags") or [], [], out)

    def test_out_of_root_candidate_is_rejected(self):
        self._create_task("T-704", "coder", "路径越界拒绝")
        outside = self.root.parent / "outside-feedback.json"
        outside.write_text(
            json.dumps({"lessons": ["outside should not be loaded"]}, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": True,
                "timeoutMs": 200,
                "maxItems": 3,
                "sourceCandidates": ["../outside-feedback.json"],
            }
        )

        out, _ = self._dispatch_once("T-704", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertNotIn("outside should not be loaded", prompt, out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertTrue(knowledge.get("degraded"), out)
        self.assertIn("rejected", str(knowledge.get("degradeReason") or "").lower(), out)

    def test_broken_first_source_falls_back_to_next_available(self):
        self._create_task("T-705", "coder", "损坏源回退")
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "bad.json").write_text("{not-valid-json", encoding="utf-8")
        (state_dir / "good.json").write_text(
            json.dumps({"lessons": ["fallback source works"]}, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": True,
                "timeoutMs": 200,
                "maxItems": 3,
                "maxRetries": 1,
                "sourceCandidates": ["state/bad.json", "state/good.json"],
            }
        )

        out, _ = self._dispatch_once("T-705", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertIn("fallback source works", prompt, out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertFalse(knowledge.get("degraded"), out)
        self.assertEqual(knowledge.get("knowledgeTags") or [], ["lessons"], out)

    def test_readonly_false_degrades_cleanly(self):
        self._create_task("T-706", "coder", "只读关闭降级")
        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": False,
                "timeoutMs": 200,
                "maxItems": 3,
                "sourceCandidates": ["state/knowledge-feedback.json"],
            }
        )

        out, _ = self._dispatch_once("T-706", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertNotIn("KNOWLEDGE_HINTS", prompt, out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertTrue(knowledge.get("degraded"), out)
        self.assertIn("readonly=true", str(knowledge.get("degradeReason") or "").lower(), out)

    def test_spawn_blocked_backfills_feedback_file(self):
        self._create_task("T-707", "coder", "失败回填")
        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": True,
                "timeoutMs": 200,
                "maxItems": 3,
                "sourceCandidates": ["state/knowledge-feedback.json"],
            }
        )

        out = _run_json(
            [
                "python3",
                str(MILE),
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                "T-707",
                "--agent",
                "coder",
                "--mode",
                "dry-run",
                "--spawn",
                "--spawn-output",
                '{"status":"blocked","message":"pytest failed tests/test_foo.py"}',
            ]
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual((out.get("spawn") or {}).get("decision"), "blocked", out)

        feedback_path = self.root / "state" / "knowledge-feedback.json"
        self.assertTrue(feedback_path.exists(), out)
        payload = json.loads(feedback_path.read_text(encoding="utf-8"))
        mistakes = payload.get("mistakes") if isinstance(payload.get("mistakes"), list) else []
        patterns = payload.get("patterns") if isinstance(payload.get("patterns"), list) else []
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        self.assertTrue(mistakes, payload)
        self.assertTrue(patterns, payload)
        self.assertIn("dispatch_failure", tags, payload)

    def test_retry_reads_same_source_after_transient_failure(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        source_path = state_dir / "transient.json"
        source_path.write_text(
            json.dumps({"lessons": ["retry success"]}, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_feedback_config(
            {
                "enabled": True,
                "readOnly": True,
                "timeoutMs": 500,
                "maxItems": 3,
                "maxRetries": 1,
                "sourceCandidates": ["state/transient.json"],
            }
        )

        adapter = _load_module(ADAPTER, "knowledge_adapter_module_for_retry_test")
        real_open = open
        injected = {"count": 0}

        def flaky_open(path, *args, **kwargs):
            mode = kwargs.get("mode", args[0] if args else "r")
            same_target = os.path.realpath(str(path)) == os.path.realpath(str(source_path))
            if same_target and "r" in str(mode) and injected["count"] == 0:
                injected["count"] += 1
                raise OSError("transient read failure")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=flaky_open):
            payload = adapter.fetch_feedback(str(self.root), task_id="T-708", agent="coder", objective="retry")
        self.assertFalse(payload.get("degraded"), payload)
        self.assertIn("retry success", payload.get("hints") or [], payload)
        self.assertEqual(injected["count"], 1, payload)


if __name__ == "__main__":
    unittest.main()
