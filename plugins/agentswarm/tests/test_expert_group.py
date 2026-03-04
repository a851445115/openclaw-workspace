import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
EXPERT = REPO / "scripts" / "lib" / "expert_group.py"


def load_expert_group_module():
    spec = importlib.util.spec_from_file_location("expert_group_module_for_test", str(EXPERT))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load expert_group module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExpertGroupTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_expert_group_module()
        self.base_policy = {
            "enabled": True,
            "blockedRetriesThreshold": 2,
            "blockedDurationMinutes": 30,
            "downstreamImpactThreshold": 2,
            "highRiskReasonCodes": ["spawn_failed"],
        }

    def test_retry_limit_trigger(self):
        out = self.mod.evaluate_trigger(
            task_snapshot={"taskId": "T-001"},
            runtime_snapshot={"retryCount": 2},
            policy=self.base_policy,
        )
        self.assertTrue(out["triggered"], out)
        self.assertIn("retry_limit", out["reasons"], out)
        self.assertEqual(out["score"], 1, out)

    def test_blocked_duration_trigger(self):
        out = self.mod.evaluate_trigger(
            task_snapshot={"taskId": "T-002", "blockedDurationMinutes": 31},
            runtime_snapshot={},
            policy=self.base_policy,
        )
        self.assertTrue(out["triggered"], out)
        self.assertIn("blocked_duration", out["reasons"], out)
        self.assertEqual(out["score"], 1, out)

    def test_downstream_impact_trigger(self):
        out = self.mod.evaluate_trigger(
            task_snapshot={"taskId": "T-003", "downstreamImpact": 3},
            runtime_snapshot={},
            policy=self.base_policy,
        )
        self.assertTrue(out["triggered"], out)
        self.assertIn("downstream_impact", out["reasons"], out)
        self.assertEqual(out["score"], 1, out)

    def test_high_risk_reason_trigger(self):
        out = self.mod.evaluate_trigger(
            task_snapshot={"taskId": "T-004"},
            runtime_snapshot={"reasonCode": "spawn_failed"},
            policy=self.base_policy,
        )
        self.assertTrue(out["triggered"], out)
        self.assertIn("high_risk_reason", out["reasons"], out)
        self.assertEqual(out["score"], 1, out)

    def test_non_trigger_when_thresholds_not_met(self):
        out = self.mod.evaluate_trigger(
            task_snapshot={"taskId": "T-005", "blockedDurationMinutes": 5, "downstreamImpact": 1},
            runtime_snapshot={"retryCount": 1, "reasonCode": "blocked_signal"},
            policy=self.base_policy,
        )
        self.assertFalse(out["triggered"], out)
        self.assertEqual(out["reasons"], [], out)
        self.assertEqual(out["score"], 0, out)

    def test_load_policy_supports_root_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "enabled": False,
                "blockedRetriesThreshold": 4,
                "highRiskReasonCodes": ["timeout", "spawn_failed"],
            }
            (config_dir / "expert-group-policy.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            policy = self.mod.load_expert_group_policy(root.as_posix())

        self.assertFalse(policy["enabled"], policy)
        self.assertEqual(policy["blockedRetriesThreshold"], 4, policy)
        self.assertIn("timeout", policy["highRiskReasonCodes"], policy)
        self.assertIn("spawn_failed", policy["highRiskReasonCodes"], policy)


if __name__ == "__main__":
    unittest.main()
