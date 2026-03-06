import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKTREE_MANAGER = REPO / "scripts" / "lib" / "worktree_manager.py"


def load_worktree_manager_module():
    spec = importlib.util.spec_from_file_location("worktree_manager_module_for_test", str(WORKTREE_MANAGER))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load worktree_manager module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorktreeManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_worktree_manager_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_worktree_policy_defaults_enabled_for_runtime(self):
        policy = self.mod.load_worktree_policy(self.root.as_posix())
        self.assertTrue(policy.get("enabled"), policy)
        self.assertTrue(policy.get("cleanupOnDone"), policy)
        self.assertEqual(policy.get("branchPrefix"), "task", policy)
        self.assertEqual(policy.get("bootstrapCommands"), [], policy)

    def test_ensure_task_worktree_skips_when_disabled(self):
        out = self.mod.ensure_task_worktree(
            self.root.as_posix(),
            "T-WT-001",
            policy_override={"enabled": False},
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("skipped"), out)
        self.assertEqual(out.get("reason"), "disabled", out)
        self.assertFalse(out.get("created"), out)

    def test_ensure_task_worktree_existing_path_is_idempotent(self):
        root_dir = self.root / "task-worktrees"
        existing_path = root_dir / "task-T-WT-002"
        existing_path.mkdir(parents=True, exist_ok=True)
        calls = []

        def _runner(_cmd, _cwd):
            calls.append((_cmd, _cwd))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        out = self.mod.ensure_task_worktree(
            self.root.as_posix(),
            "T-WT-002",
            policy_override={"enabled": True, "rootDir": str(root_dir)},
            runner=_runner,
        )
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("created"), out)
        self.assertFalse(out.get("skipped"), out)
        self.assertEqual(out.get("reason"), "existing", out)
        self.assertEqual(out.get("path"), existing_path.as_posix(), out)
        self.assertEqual(calls, [], calls)

    def test_ensure_task_worktree_creates_when_missing(self):
        root_dir = self.root / "task-worktrees"
        calls = []

        def _runner(cmd, cwd):
            calls.append((list(cmd), cwd))
            text = " ".join(cmd)
            if "rev-parse --show-toplevel" in text:
                return {"returncode": 0, "stdout": f"{self.root.as_posix()}\n", "stderr": ""}
            if "rev-parse --verify" in text:
                return {"returncode": 1, "stdout": "", "stderr": "missing branch"}
            if "worktree add" in text:
                return {"returncode": 0, "stdout": "", "stderr": ""}
            return {"returncode": 0, "stdout": "", "stderr": ""}

        out = self.mod.ensure_task_worktree(
            self.root.as_posix(),
            "T-WT-003",
            policy_override={"enabled": True, "rootDir": str(root_dir), "branchPrefix": "task"},
            runner=_runner,
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("created"), out)
        self.assertFalse(out.get("skipped"), out)
        self.assertEqual(out.get("reason"), "created", out)
        self.assertEqual(out.get("branch"), "task/T-WT-003", out)
        self.assertTrue(any("worktree" in " ".join(call[0]) and "add" in " ".join(call[0]) for call in calls), calls)

    def test_cleanup_task_worktree_removes_path(self):
        root_dir = self.root / "task-worktrees"
        path = root_dir / "task-T-WT-004"
        path.mkdir(parents=True, exist_ok=True)
        calls = []

        def _runner(cmd, cwd):
            calls.append((list(cmd), cwd))
            if "worktree remove" in " ".join(cmd):
                return {"returncode": 0, "stdout": "", "stderr": ""}
            return {"returncode": 0, "stdout": "", "stderr": ""}

        out = self.mod.cleanup_task_worktree(
            self.root.as_posix(),
            "T-WT-004",
            force=True,
            policy_override={"enabled": True, "rootDir": str(root_dir)},
            runner=_runner,
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("removed"), out)
        self.assertFalse(out.get("skipped"), out)
        self.assertEqual(out.get("reason"), "removed", out)
        remove_calls = [call for call in calls if "worktree remove" in " ".join(call[0])]
        self.assertTrue(remove_calls, calls)
        self.assertIn("--force", remove_calls[0][0], remove_calls)


if __name__ == "__main__":
    unittest.main()
