import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "scripts" / "lib" / "codex_worker_bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("codex_worker_bridge_for_test", str(BRIDGE))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load codex_worker_bridge module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexWorkerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge_module()

    def test_build_schema_includes_checkpoint_contract(self):
        schema = self.bridge.build_schema()
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        checkpoint = props.get("checkpoint") if isinstance(props.get("checkpoint"), dict) else {}
        checkpoint_props = checkpoint.get("properties") if isinstance(checkpoint.get("properties"), dict) else {}
        required = checkpoint.get("required") if isinstance(checkpoint.get("required"), list) else []

        self.assertIn("checkpoint", props, schema)
        self.assertEqual(checkpoint_props.get("progressPercent", {}).get("type"), "integer", schema)
        self.assertEqual(checkpoint_props.get("completed", {}).get("type"), "array", schema)
        self.assertEqual(checkpoint_props.get("remaining", {}).get("type"), "array", schema)
        self.assertEqual(checkpoint_props.get("nextAction", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("continueHint", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("stallSignal", {}).get("type"), "string", schema)
        self.assertEqual(checkpoint_props.get("evidenceDelta", {}).get("type"), "array", schema)
        self.assertEqual(
            required,
            [
                "progressPercent",
                "completed",
                "remaining",
                "nextAction",
                "continueHint",
                "stallSignal",
                "evidenceDelta",
            ],
            schema,
        )

    def test_normalize_result_preserves_checkpoint_payload(self):
        result = self.bridge.normalize_result(
            "T-CP",
            "debugger",
            {
                "status": "progress",
                "summary": "checkpoint update",
                "checkpoint": {
                    "progressPercent": 52,
                    "completed": ["parsed failure logs"],
                    "remaining": ["add continuation logic"],
                    "nextAction": "patch milestones",
                    "continueHint": "continue",
                    "stallSignal": "none",
                    "evidenceDelta": ["traceback signature collected"],
                },
            },
        )

        checkpoint = result.get("checkpoint") if isinstance(result.get("checkpoint"), dict) else {}
        self.assertEqual(checkpoint.get("progressPercent"), 52, result)
        self.assertEqual(checkpoint.get("completed"), ["parsed failure logs"], result)
        self.assertEqual(checkpoint.get("remaining"), ["add continuation logic"], result)
        self.assertEqual(checkpoint.get("nextAction"), "patch milestones", result)
        self.assertEqual(checkpoint.get("continueHint"), "continue", result)
        self.assertEqual(checkpoint.get("stallSignal"), "none", result)
        self.assertEqual(checkpoint.get("evidenceDelta"), ["traceback signature collected"], result)


if __name__ == "__main__":
    unittest.main()
