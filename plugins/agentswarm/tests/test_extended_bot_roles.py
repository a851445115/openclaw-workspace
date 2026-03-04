import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
INIT = SCRIPTS / "init-task-board"


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class ExtendedBotRolesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_autopilot_preserves_knowledge_curator_assignee(self):
        run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "@knowledge-curator create task T-901: 整理知识反馈条目",
            ]
        )

        out = run_json(
            [
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
                '{"status":"done","message":"已完成，证据: logs/knowledge-curator.log"}',
            ]
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["stepsRun"], 1, out)
        self.assertEqual(out["steps"][0]["agent"], "knowledge-curator", out)
        self.assertEqual(out["steps"][0]["dispatch"]["agent"], "knowledge-curator", out)

    def test_dispatch_uses_real_mention_for_paper_summarizer(self):
        run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "@paper-summarizer create task T-902: 总结最新论文",
            ]
        )

        out = run_json(
            [
                "python3",
                str(MILE),
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                "T-902",
                "--agent",
                "paper-summarizer",
                "--mode",
                "dry-run",
            ]
        )
        self.assertTrue(out["ok"], out)
        task_text = out["taskSend"]["payload"]["text"]
        self.assertIn('<at user_id="ou_b68a4992ef0f069828e6c3938b7cff34">', task_text, out)


if __name__ == "__main__":
    unittest.main()
