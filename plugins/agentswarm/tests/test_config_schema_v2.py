import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PLUGIN_JSON = REPO / "openclaw.plugin.json"
RUNTIME = REPO / "scripts" / "lib" / "config_runtime.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigSchemaV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        self.runtime = _load_module(RUNTIME, "config_runtime_module_for_schema_v2_tests")

    def tearDown(self):
        self.tmp.cleanup()

    def test_plugin_schema_contains_v2_fields_and_backward_compatible_agents(self):
        raw = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
        schema = raw.get("configSchema") if isinstance(raw, dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        orchestrator = props.get("orchestrator") if isinstance(props.get("orchestrator"), dict) else {}
        orchestrator_props = orchestrator.get("properties") if isinstance(orchestrator.get("properties"), dict) else {}
        agents = props.get("agents") if isinstance(props.get("agents"), dict) else {}
        items = agents.get("items")
        item_modes = items.get("oneOf") if isinstance(items, dict) else []

        self.assertIn("retryPolicy", orchestrator_props, raw)
        self.assertIn("budgetPolicy", orchestrator_props, raw)
        self.assertTrue(isinstance(item_modes, list) and len(item_modes) >= 2, raw)

    def test_load_old_config_uses_defaults_and_backward_compat_fallback(self):
        old = {
            "agents": ["coder", "debugger"],
            "orchestrator": {"maxConcurrentSpawns": 2},
        }
        loaded = self.runtime.load_runtime_config(str(self.root), override=old)
        self.assertEqual(loaded.get("agents"), [{"id": "coder", "capabilities": []}, {"id": "debugger", "capabilities": []}], loaded)
        orchestrator = loaded.get("orchestrator") if isinstance(loaded.get("orchestrator"), dict) else {}
        retry = orchestrator.get("retryPolicy") if isinstance(orchestrator.get("retryPolicy"), dict) else {}
        backoff = retry.get("backoff") if isinstance(retry.get("backoff"), dict) else {}
        budget = orchestrator.get("budgetPolicy") if isinstance(orchestrator.get("budgetPolicy"), dict) else {}
        guardrails = budget.get("guardrails") if isinstance(budget.get("guardrails"), dict) else {}
        self.assertEqual(orchestrator.get("maxConcurrentSpawns"), 2, loaded)
        self.assertGreaterEqual(int(retry.get("maxAttempts") or 0), 1, loaded)
        self.assertIn(backoff.get("mode"), {"fixed", "linear", "exponential"}, loaded)
        self.assertGreaterEqual(int(guardrails.get("maxTaskTokens") or 0), 1, loaded)

    def test_load_mixed_config_merges_v2_and_old_shapes(self):
        mixed = {
            "agents": [
                "coder",
                {"id": "debugger", "capabilities": ["triage", "logs"]},
            ],
            "orchestrator": {
                "retryPolicy": {"maxAttempts": 4},
                "budgetPolicy": {"guardrails": {"maxTaskTokens": 999}},
            },
        }
        loaded = self.runtime.load_runtime_config(str(self.root), override=mixed)
        self.assertEqual(loaded.get("agents"), [{"id": "coder", "capabilities": []}, {"id": "debugger", "capabilities": ["triage", "logs"]}], loaded)
        orchestrator = loaded.get("orchestrator") if isinstance(loaded.get("orchestrator"), dict) else {}
        retry = orchestrator.get("retryPolicy") if isinstance(orchestrator.get("retryPolicy"), dict) else {}
        backoff = retry.get("backoff") if isinstance(retry.get("backoff"), dict) else {}
        budget = orchestrator.get("budgetPolicy") if isinstance(orchestrator.get("budgetPolicy"), dict) else {}
        guardrails = budget.get("guardrails") if isinstance(budget.get("guardrails"), dict) else {}
        self.assertEqual(retry.get("maxAttempts"), 4, loaded)
        self.assertIsInstance(backoff, dict, loaded)
        self.assertEqual(guardrails.get("maxTaskTokens"), 999, loaded)
        self.assertGreaterEqual(int(guardrails.get("maxTaskRetries") or 0), 1, loaded)

    def test_load_v2_full_config_preserves_explicit_values(self):
        full = {
            "agents": [
                {"id": "coder", "capabilities": ["code", "tests"]},
                {"id": "invest-analyst", "capabilities": ["research", "citations"]},
            ],
            "orchestrator": {
                "maxConcurrentSpawns": 6,
                "retryPolicy": {
                    "maxAttempts": 5,
                    "backoff": {
                        "mode": "exponential",
                        "baseMs": 200,
                        "maxMs": 2000,
                        "multiplier": 2.5,
                        "jitterPct": 10,
                    },
                },
                "budgetPolicy": {
                    "guardrails": {
                        "maxTaskTokens": 3210,
                        "maxTaskWallTimeSec": 456,
                        "maxTaskRetries": 3,
                    }
                },
            },
        }
        loaded = self.runtime.load_runtime_config(str(self.root), override=full)
        self.assertEqual(loaded.get("agents"), full["agents"], loaded)
        self.assertEqual(loaded.get("orchestrator"), full["orchestrator"], loaded)


if __name__ == "__main__":
    unittest.main()
