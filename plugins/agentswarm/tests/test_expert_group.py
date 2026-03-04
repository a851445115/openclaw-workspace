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

    def test_build_expert_templates_includes_roles_and_required_fields(self):
        templates = self.mod.build_expert_templates(
            reasons=["retry_limit", "blocked_duration"],
            task_snapshot={"taskId": "T-006"},
            runtime_snapshot={"reasonCode": "blocked_signal"},
        )
        self.assertIsInstance(templates, list, templates)
        self.assertGreaterEqual(len(templates), 3, templates)
        roles = {str(item.get("role") or "") for item in templates if isinstance(item, dict)}
        self.assertTrue({"coder", "debugger", "invest-analyst"}.issubset(roles), templates)
        for item in templates:
            self.assertIn("task", item, item)
            self.assertIsInstance(item.get("task"), str, item)
            required_fields = item.get("requiredFields")
            self.assertIsInstance(required_fields, list, item)
            for field in ("hypothesis", "evidence", "confidence", "proposedFix", "risk"):
                self.assertIn(field, required_fields, item)

    def test_build_expert_templates_handles_unknown_reasons_with_generic_task(self):
        templates = self.mod.build_expert_templates(
            reasons=["not_in_policy_reason"],
            task_snapshot={"taskId": "T-099"},
            runtime_snapshot={},
        )
        self.assertTrue(templates, templates)
        first = templates[0]
        self.assertIn("task", first, first)
        self.assertIn("not_in_policy_reason", first.get("task") or "", first)

    def test_converge_expert_conclusions_empty_input_returns_stable_defaults(self):
        out = self.mod.converge_expert_conclusions([])
        self.assertIsInstance(out, dict, out)
        self.assertIn("consensusPlan", out, out)
        self.assertIn("owner", out, out)
        self.assertIn("executionChecklist", out, out)
        self.assertIn("acceptanceGate", out, out)
        self.assertIsInstance(out.get("executionChecklist"), list, out)
        self.assertIsInstance(out.get("acceptanceGate"), list, out)

    def test_converge_expert_conclusions_prefers_high_confidence_fix(self):
        out = self.mod.converge_expert_conclusions(
            [
                {
                    "role": "coder",
                    "confidence": 0.4,
                    "hypothesis": "guard condition missing",
                    "proposedFix": "add a null-check in parser",
                    "evidence": "stack trace from parser.py",
                },
                {
                    "role": "debugger",
                    "confidence": 0.92,
                    "hypothesis": "race condition in retry cache",
                    "proposedFix": "serialize cache writes with lock",
                    "risk": "may slightly increase latency",
                },
                {
                    "role": "invest-analyst",
                    "hypothesis": "downstream dependency mismatch",
                },
            ]
        )
        self.assertEqual(out.get("owner"), "debugger", out)
        self.assertIn("serialize cache writes with lock", out.get("consensusPlan") or "", out)
        self.assertGreater(len(out.get("executionChecklist") or []), 0, out)
        self.assertGreater(len(out.get("acceptanceGate") or []), 0, out)

    def test_build_expert_templates_accepts_alias_roles_but_outputs_canonical(self):
        templates = self.mod.build_expert_templates(
            reasons=["retry_limit"],
            task_snapshot={"taskId": "T-010"},
            runtime_snapshot={},
            roles=["coder", "Analyst", "invest_analyst", "debugger"],
        )
        roles = [str(item.get("role") or "") for item in templates if isinstance(item, dict)]
        self.assertIn("invest-analyst", roles, templates)
        self.assertNotIn("analyst", roles, templates)
        self.assertEqual(roles.count("invest-analyst"), 1, templates)

    def test_lifecycle_transition_create_execute_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = self.mod.transition_lifecycle_state(
                root=tmp,
                task_id="T-900",
                target_status="created",
                reasons=["retry_limit"],
                templates=[{"role": "coder", "task": "triage"}],
                consensus={"consensusPlan": "collect logs", "owner": "coder"},
            )
            self.assertEqual(created.get("status"), "created", created)
            group_id = created.get("groupId")
            self.assertTrue(group_id, created)

            executing = self.mod.transition_lifecycle_state(
                root=tmp,
                task_id="T-900",
                target_status="executing",
                reasons=["retry_limit"],
                templates=[{"role": "debugger", "task": "trace"}],
                consensus={"consensusPlan": "run debugger", "owner": "debugger"},
            )
            self.assertEqual(executing.get("status"), "executing", executing)

            archived = self.mod.transition_lifecycle_state(
                root=tmp,
                task_id="T-900",
                target_status="archived",
                reasons=[],
                templates=[],
                consensus={"consensusPlan": "", "owner": "orchestrator"},
            )
            self.assertEqual(archived.get("status"), "archived", archived)
            history = archived.get("history") or []
            self.assertEqual(len(history), 3, archived)
            self.assertEqual(history[0].get("to"), "created", archived)
            self.assertEqual(history[1].get("to"), "executing", archived)
            self.assertEqual(history[2].get("to"), "archived", archived)

            lifecycle_path = Path(tmp) / "state" / "expert-groups" / f"{group_id}.json"
            self.assertTrue(lifecycle_path.exists(), lifecycle_path)

    def test_lifecycle_transition_tolerates_corrupt_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            group_id = self.mod.build_lifecycle_group_id("T-901")
            lifecycle_path = Path(tmp) / "state" / "expert-groups" / f"{group_id}.json"
            lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
            lifecycle_path.write_text("{bad json", encoding="utf-8")

            out = self.mod.transition_lifecycle_state(
                root=tmp,
                task_id="T-901",
                target_status="created",
                reasons=["blocked_duration"],
                templates=[],
                consensus={"consensusPlan": "", "owner": "orchestrator"},
            )
            self.assertEqual(out.get("status"), "created", out)
            self.assertEqual(len(out.get("history") or []), 1, out)


if __name__ == "__main__":
    unittest.main()
