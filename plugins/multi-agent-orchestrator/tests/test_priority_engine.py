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
        self.priority_engine = self.milestones.priority_engine

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
        self.assertNotEqual(picked.get("taskId"), "T-001", picked)
        self.assertIn("_prioritySelection", picked, picked)
        selection = picked.get("_prioritySelection") or {}
        ready_top = selection.get("readyQueueTop") or []
        ready_ids = [str(x.get("taskId") or "") for x in ready_top if isinstance(x, dict)]
        self.assertNotIn("T-001", ready_ids, selection)

    def test_requested_task_not_ready_is_rejected(self):
        tasks = {
            "T-100": {
                "taskId": "T-100",
                "title": "requested but blocked by dependency",
                "status": "pending",
                "dependsOn": ["T-101"],
                "priority": 9,
                "impact": 9,
            },
            "T-101": {
                "taskId": "T-101",
                "title": "dependency still running",
                "status": "in_progress",
            },
            "T-102": {
                "taskId": "T-102",
                "title": "ready task",
                "status": "pending",
                "priority": 1,
                "impact": 1,
            },
        }
        selected = self.priority_engine.select_task(tasks, requested_task_id="T-100")
        self.assertEqual(selected.get("selectedTaskId"), "", selected)
        self.assertIsNone(selected.get("selectedTask"), selected)
        self.assertEqual(selected.get("reasonCode"), "requested_task_not_ready", selected)

        write_snapshot(self.root, tasks)
        picked = self.milestones.choose_task_for_run(str(self.root), "T-100")
        self.assertIsNone(picked)

    def test_blocked_by_non_t_numeric_task_id_can_be_resolved(self):
        write_snapshot(
            self.root,
            {
                "BUG-1": {
                    "taskId": "BUG-1",
                    "title": "blocking bug",
                    "status": "in_progress",
                },
                "T-200": {
                    "taskId": "T-200",
                    "title": "blocked by BUG-1",
                    "status": "pending",
                    "blockedBy": ["BUG-1"],
                    "priority": 7,
                    "impact": 7,
                },
                "T-201": {
                    "taskId": "T-201",
                    "title": "always ready fallback",
                    "status": "pending",
                    "priority": 1,
                    "impact": 1,
                },
            },
        )
        first = self.milestones.choose_task_for_run(str(self.root), "")
        self.assertIsNotNone(first)
        self.assertEqual(first.get("taskId"), "T-201", first)

        write_snapshot(
            self.root,
            {
                "BUG-1": {
                    "taskId": "BUG-1",
                    "title": "blocking bug",
                    "status": "done",
                },
                "T-200": {
                    "taskId": "T-200",
                    "title": "blocked by BUG-1",
                    "status": "pending",
                    "blockedBy": ["BUG-1"],
                    "priority": 7,
                    "impact": 7,
                },
                "T-201": {
                    "taskId": "T-201",
                    "title": "always ready fallback",
                    "status": "pending",
                    "priority": 1,
                    "impact": 1,
                },
            },
        )
        second = self.milestones.choose_task_for_run(str(self.root), "")
        self.assertIsNotNone(second)
        self.assertEqual(second.get("taskId"), "T-200", second)

    def test_non_finite_priority_or_impact_cannot_beat_finite_high_score(self):
        tasks = {
            "T-300": {
                "taskId": "T-300",
                "title": "finite high score",
                "status": "pending",
                "priority": 9,
                "impact": 9,
            },
            "T-301": {
                "taskId": "T-301",
                "title": "infinite score should be sanitized",
                "status": "pending",
                "priority": float("inf"),
                "impact": 0,
            },
            "T-302": {
                "taskId": "T-302",
                "title": "nan score should be sanitized",
                "status": "pending",
                "priority": "NaN",
                "impact": 0,
            },
        }
        selected = self.priority_engine.select_task(tasks)
        self.assertEqual(selected.get("selectedTaskId"), "T-300", selected)

    def test_choose_task_fallback_keeps_dependency_aware_filtering(self):
        write_snapshot(
            self.root,
            {
                "T-400": {
                    "taskId": "T-400",
                    "title": "blocked in fallback path",
                    "status": "pending",
                    "dependsOn": ["T-401"],
                    "priority": 10,
                    "impact": 10,
                },
                "T-401": {
                    "taskId": "T-401",
                    "title": "dependency not done",
                    "status": "in_progress",
                },
                "T-402": {
                    "taskId": "T-402",
                    "title": "ready fallback candidate",
                    "status": "pending",
                    "priority": 1,
                    "impact": 1,
                },
            },
        )
        original_select = self.milestones.priority_engine.select_task

        def boom(*_args, **_kwargs):
            raise RuntimeError("forced priority engine failure")

        self.milestones.priority_engine.select_task = boom
        try:
            picked = self.milestones.choose_task_for_run(str(self.root), "")
            self.assertIsNotNone(picked)
            self.assertEqual(picked.get("taskId"), "T-402", picked)

            requested = self.milestones.choose_task_for_run(str(self.root), "T-400")
            self.assertIsNone(requested)
        finally:
            self.milestones.priority_engine.select_task = original_select

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

    def test_task_board_create_has_priority_dependency_fields(self):
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
                "@coder create task T-030: defaults field coverage",
            ]
        )
        status = run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "status T-030",
            ]
        )
        task = status.get("task") or {}
        self.assertIn("dependsOn", task, task)
        self.assertIn("blockedBy", task, task)
        self.assertIn("priority", task, task)
        self.assertIn("impact", task, task)
        self.assertEqual(task.get("dependsOn"), [], task)
        self.assertEqual(task.get("blockedBy"), [], task)
        self.assertEqual(task.get("priority"), 0, task)
        self.assertEqual(task.get("impact"), 0, task)

    def test_task_board_non_finite_priority_and_impact_fallback_to_default(self):
        write_snapshot(
            self.root,
            {
                "T-500": {
                    "taskId": "T-500",
                    "title": "non finite values",
                    "status": "pending",
                    "priority": float("nan"),
                    "impact": float("inf"),
                }
            },
        )
        status = run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "status T-500",
            ]
        )
        task = status.get("task") or {}
        self.assertEqual(task.get("priority"), 0, task)
        self.assertEqual(task.get("impact"), 0, task)


if __name__ == "__main__":
    unittest.main()
