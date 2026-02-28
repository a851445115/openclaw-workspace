import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BOARD = SCRIPTS / "lib" / "task_board.py"
MILE = SCRIPTS / "lib" / "milestones.py"
INIT = SCRIPTS / "init-task-board"
REBUILD = SCRIPTS / "rebuild-snapshot"
RECOVER = SCRIPTS / "recover-stale-locks"
INBOUND = SCRIPTS / "feishu-inbound-router"


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatch_spawn_closes_task_done(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-001: 完成闭环",
        ])

        dispatch = run_json([
            "python3",
            str(MILE),
            "dispatch",
            "--root",
            str(self.root),
            "--task-id",
            "T-001",
            "--agent",
            "coder",
            "--mode",
            "dry-run",
            "--spawn-output",
            '{"status":"done","message":"T-001 已完成，证据: logs/run.log"}',
        ])
        self.assertTrue(dispatch["ok"], dispatch)
        self.assertTrue(dispatch["autoClose"], dispatch)
        self.assertEqual(dispatch["spawn"]["decision"], "done", dispatch)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-001",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_feishu_router_handles_claim_done_commands(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-002: 命令入口测试",
        ])

        claim = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator claim T-002",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(claim["ok"], claim)

        done = run_json([
            "python3",
            str(MILE),
            "feishu-router",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@orchestrator done T-002: 已完成，证据: docs/protocol.md",
            "--mode",
            "dry-run",
        ])
        self.assertTrue(done["ok"], done)

        status = run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "status T-002",
        ])
        self.assertEqual(status["task"]["status"], "done", status)

    def test_clarify_global_throttle(self):
        state_file = self.root / "state" / "clarify.cooldown.json"
        now_ts = int(time.time())
        state_file.write_text(
            json.dumps(
                {
                    "entries": {
                        "oc_041146c92a9ccb403a7f4f48fb59701d:*": {
                            "ts": now_ts,
                            "at": "2026-02-28T00:00:00Z",
                            "taskId": "T-001",
                            "by": "orchestrator",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        second_proc = subprocess.run(
            [
                "python3",
                str(MILE),
                "clarify",
                "--root",
                str(self.root),
                "--task-id",
                "T-003",
                "--role",
                "debugger",
                "--question",
                "请提供错误栈",
                "--mode",
                "dry-run",
                "--state-file",
                str(state_file),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(second_proc.returncode, 0, second_proc.stdout + second_proc.stderr)
        payload = json.loads(second_proc.stdout.strip())
        self.assertTrue(payload.get("throttled"), payload)

    def test_rebuild_and_recover_scripts(self):
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "@coder create task T-004: rebuild",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "coder",
            "--text",
            "@coder claim task T-004",
        ])
        run_json([
            "python3",
            str(BOARD),
            "apply",
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "mark done T-004: done",
        ])

        compact_out = self.root / "state" / "tasks.compacted.jsonl"
        rebuild = run_json([
            str(REBUILD),
            "--root",
            str(self.root),
            "--apply",
            "--compact-jsonl",
            str(compact_out),
        ])
        self.assertTrue(rebuild["ok"], rebuild)
        self.assertTrue(compact_out.exists(), rebuild)

        lock_dir = self.root / "state" / "locks"
        stale = lock_dir / "manual.lock"
        stale.write_text(
            json.dumps(
                {
                    "owner": "test",
                    "pid": 999999,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "createdAtTs": int(time.time()) - 3600,
                    "expiresAtTs": int(time.time()) - 1800,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        dry = run_json([str(RECOVER), "--root", str(self.root), "--dry-run"])
        self.assertTrue(any(c["path"].endswith("manual.lock") for c in dry["candidates"]), dry)

        apply = run_json([str(RECOVER), "--root", str(self.root), "--apply"])
        self.assertTrue(apply["ok"], apply)
        self.assertFalse(stale.exists(), apply)

    def test_inbound_ignores_bot_loop(self):
        out = run_json([
            "python3",
            str(INBOUND),
            "--root",
            str(self.root),
            "--actor",
            "orchestrator",
            "--text",
            "[DONE] T-888 | 状态=已完成",
            "--milestones",
            "dry-run",
        ])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["router"].get("intent"), "ignored_loop", out)


if __name__ == "__main__":
    unittest.main()
