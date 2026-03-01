import importlib.util
import json
import math
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OPS_METRICS = REPO / "scripts" / "lib" / "ops_metrics.py"
EXPORT = REPO / "scripts" / "export-weekly-ops-report"


def load_ops_metrics_module():
    spec = importlib.util.spec_from_file_location("ops_metrics_module_for_test", str(OPS_METRICS))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load ops_metrics module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ObservabilityMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_path = self.state_dir / "ops.metrics.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _iso(self, dt: datetime) -> str:
        return dt.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _write_events(self, rows):
        with self.metrics_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

    def test_aggregate_metrics_with_timeframe_filter(self):
        ops_metrics = load_ops_metrics_module()
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)
        rows = [
            {"event": "dispatch_done", "at": self._iso(now - timedelta(hours=2)), "taskId": "T-001", "cycleMs": 1000},
            {
                "event": "dispatch_blocked",
                "at": self._iso(now - timedelta(hours=2)),
                "taskId": "T-002",
                "reasonCode": "incomplete_output",
                "cycleMs": 2000,
            },
            {
                "event": "dispatch_blocked",
                "at": self._iso(now - timedelta(hours=1)),
                "taskId": "T-003",
                "reasonCode": "budget_exceeded",
                "cycleMs": 4000,
            },
            {"event": "recovery_scheduled", "at": self._iso(now - timedelta(minutes=40)), "taskId": "T-002"},
            {"event": "recovery_escalated", "at": self._iso(now - timedelta(minutes=35)), "taskId": "T-003"},
            {"event": "scheduler_tick", "at": self._iso(now - timedelta(minutes=20)), "action": "tick"},
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=10)), "taskId": "T-004", "cycleMs": 3000},
            {"event": "dispatch_done", "at": self._iso(old), "taskId": "T-999", "cycleMs": 9999},
        ]
        self._write_events(rows)

        summary = ops_metrics.aggregate_metrics(str(self.root), days=7, now_ts=now.timestamp())

        self.assertEqual(summary["throughputCompleted"], 2, summary)
        self.assertAlmostEqual(summary["successRate"], 0.5, places=6, msg=str(summary))
        self.assertEqual(
            summary["blockedReasonDistribution"],
            {"incomplete_output": 1, "budget_exceeded": 1},
            summary,
        )
        self.assertAlmostEqual(summary["recoveryRate"], 0.5, places=6, msg=str(summary))
        self.assertAlmostEqual(summary["averageCycleMs"], 2500.0, places=6, msg=str(summary))
        self.assertEqual(summary["counts"]["schedulerTick"], 1, summary)
        self.assertEqual(summary["eventsConsidered"], 7, summary)

    def test_export_weekly_ops_report_script_runs(self):
        now = datetime.now(timezone.utc)
        rows = [
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=3)), "taskId": "T-101", "cycleMs": 1200},
            {
                "event": "dispatch_blocked",
                "at": self._iso(now - timedelta(minutes=2)),
                "taskId": "T-102",
                "reasonCode": "budget_exceeded",
                "cycleMs": 800,
            },
            {"event": "recovery_escalated", "at": self._iso(now - timedelta(minutes=1)), "taskId": "T-102"},
        ]
        self._write_events(rows)

        proc = subprocess.run(
            ["python3", str(EXPORT), "--root", str(self.root), "--days", "7"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload.get("ok"), payload)
        report = payload.get("report") or {}
        self.assertEqual(report.get("throughputCompleted"), 1, payload)
        self.assertIn("successRate", report, payload)
        self.assertIn("blockedReasonDistribution", report, payload)

    def test_aggregate_metrics_filters_non_finite_values_and_invalid_ts(self):
        ops_metrics = load_ops_metrics_module()
        now = datetime.now(timezone.utc)
        rows = [
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=9)), "taskId": "T-201", "cycleMs": 1500},
            {"event": "dispatch_done", "ts": "NaN", "at": self._iso(now - timedelta(minutes=8)), "taskId": "T-202", "cycleMs": 500},
            {
                "event": "dispatch_blocked",
                "ts": "Infinity",
                "at": self._iso(now - timedelta(minutes=7)),
                "taskId": "T-203",
                "reasonCode": "incomplete_output",
                "cycleMs": 800,
            },
            {
                "event": "dispatch_blocked",
                "at": self._iso(now - timedelta(minutes=6)),
                "taskId": "T-204",
                "reasonCode": "budget_exceeded",
                "cycleMs": "nan",
            },
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=5)), "taskId": "T-205", "cycleMs": "inf"},
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=4)), "taskId": "T-206", "cycleMs": "-inf"},
            {"event": "dispatch_done", "at": self._iso(now - timedelta(minutes=3)), "taskId": "T-207", "cycleMs": "oops"},
        ]
        self._write_events(rows)

        summary = ops_metrics.aggregate_metrics(str(self.root), days=7, now_ts=now.timestamp())

        self.assertEqual(summary["eventsConsidered"], 5, summary)
        self.assertEqual(summary["throughputCompleted"], 4, summary)
        self.assertEqual(summary["blockedReasonDistribution"], {"budget_exceeded": 1}, summary)
        self.assertTrue(math.isfinite(summary["averageCycleMs"]), summary)
        self.assertEqual(summary["averageCycleMs"], 1500.0, summary)


if __name__ == "__main__":
    unittest.main()
