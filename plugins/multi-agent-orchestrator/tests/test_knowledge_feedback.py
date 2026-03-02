import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
MILE = REPO / "scripts" / "lib" / "milestones.py"
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
        config_dir = self.root / "config"
        state_dir = self.root / "state"
        config_dir.mkdir(parents=True, exist_ok=True)
        state_dir.mkdir(parents=True, exist_ok=True)

        (config_dir / "knowledge-feedback.json").write_text(
            json.dumps(
                {
                    "enabled": True,
                    "readOnly": True,
                    "timeoutMs": 200,
                    "maxItems": 3,
                    "sourceCandidates": ["state/knowledge-feedback.json"],
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
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
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "knowledge-feedback.json").write_text(
            json.dumps(
                {
                    "enabled": False,
                    "readOnly": True,
                    "timeoutMs": 200,
                    "maxItems": 3,
                    "sourceCandidates": ["state/not-exist.json"],
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        out, _ = self._dispatch_once("T-703", "coder")
        prompt = str(out.get("agentPrompt") or "")
        self.assertNotIn("KNOWLEDGE_HINTS", prompt, out)
        knowledge = out.get("knowledge") if isinstance(out.get("knowledge"), dict) else {}
        self.assertFalse(knowledge.get("degraded"), out)
        self.assertEqual(knowledge.get("knowledgeTags") or [], [], out)


if __name__ == "__main__":
    unittest.main()
