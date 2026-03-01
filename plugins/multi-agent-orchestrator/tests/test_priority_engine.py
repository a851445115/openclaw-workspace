import importlib.util
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


def load_milestones_module():
    spec = importlib.util.spec_from_file_location("milestones_module_for_priority_tests", str(MILE))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load milestones module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


def write_snapshot(root: Path, tasks: dict):
    snap = root / "state" / "tasks.snapshot.json"
    snap.write_text(
        json.dumps(
            {
                "tasks": tasks,
                "meta": {"version": 2, "updatedAt": "2026-03-01T00:00:00Z"},
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class PriorityEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)
        self.milestones = load_milestones_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unmet_dependency_is_not_selected(self):
        write_snapshot(
            self.root,
            {
                "T-001": {
                    "taskId": "T-001",
                    "title": "blocked by dependency",
                    "status": "pending",
                    "dependsOn": ["T-002"],
                    "priority": 10,
                    "impact": 10,
                },
                "T-002": {
                    "taskId": "T-002",
                    "title": "dependency not done",
                    "status": "in_progress",
                    "priority": 1,
                    "impact": 1,
                },
                "T-003": {
                    "taskId": "T-003",
                    "title": "ready fallback",
                    "status": "pending",
                    "priority": 1,
                    "impact": 1,
                },
            },
        )

        picked = self.milestones.choose_task_for_run(str(self.root), "")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.get("taskId"), "T-003", picked)
        self.assertIn("_prioritySelection", picked, picked)

    def test_decision_is_reproducible_for_same_snapshot(self):
        write_snapshot(
            self.root,
            {
                "T-010": {
                    "taskId": "T-010",
                    "title": "same score A",
                    "status": "pending",
                    "priority": 5,
                    "impact": 5,
                },
                "T-011": {
                    "taskId": "T-011",
                    "title": "same score B",
                    "status": "pending",
                    "priority": 5,
                    "impact": 5,
                },
            },
        )

        picked_ids = []
        scores = []
        for _ in range(5):
            picked = self.milestones.choose_task_for_run(str(self.root), "")
            self.assertIsNotNone(picked)
            picked_ids.append(str(picked.get("taskId") or ""))
            selection = picked.get("_prioritySelection") or {}
            scores.append(selection.get("score"))

        self.assertEqual(len(set(picked_ids)), 1, picked_ids)
        self.assertEqual(picked_ids[0], "T-010", picked_ids)
        self.assertTrue(all(isinstance(v, (int, float)) for v in scores), scores)

    def test_autopilot_dispatch_regression_and_selection_explainability(self):
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
                "@coder create task T-020: regression a",
            ]
        )
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
                "@coder create task T-021: regression b",
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
                "2",
                "--spawn-output",
                '{"status":"done","summary":"done","evidence":["logs/priority.log"]}',
            ]
        )
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("stepsRun"), 2, out)
        steps = out.get("steps") or []
        self.assertEqual(len(steps), 2, out)
        for step in steps:
            self.assertTrue((step.get("dispatch") or {}).get("ok"), step)
            selection = step.get("selection") or {}
            self.assertIn("score", selection, step)
            self.assertIn("reason", selection, step)

        t20 = run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "status T-020",
            ]
        )
        t21 = run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "status T-021",
            ]
        )
        self.assertEqual((t20.get("task") or {}).get("status"), "done", t20)
        self.assertEqual((t21.get("task") or {}).get("status"), "done", t21)


if __name__ == "__main__":
    unittest.main()
