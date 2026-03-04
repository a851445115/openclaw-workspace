import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
STRATEGY_LIB = REPO / "scripts" / "lib" / "strategy_library.py"
MILE = REPO / "scripts" / "lib" / "milestones.py"
BOARD = REPO / "scripts" / "lib" / "task_board.py"
INIT = REPO / "scripts" / "init-task-board"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise AssertionError(f"failed to load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoleStrategyLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_strategy_config(self, payload):
        config_dir = self.root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "role-strategies.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _run_json(self, cmd):
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        try:
            return json.loads(proc.stdout.strip())
        except Exception as err:
            raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")

    def test_resolve_strategy_priority_and_fallback(self):
        self._write_strategy_config(
            {
                "taskKinds": {
                    "coding": {
                        "coder": {
                            "strategyId": "coding-coder-v1",
                            "content": "coding+coder",
                            "enabled": True,
                            "rolloutPercent": 100,
                        },
                        "default": {
                            "strategyId": "coding-default-v1",
                            "content": "coding default",
                            "enabled": True,
                            "rolloutPercent": 100,
                        },
                    }
                },
                "agents": {
                    "coder": {
                        "default": {
                            "strategyId": "agent-coder-v1",
                            "content": "agent default",
                            "enabled": True,
                            "rolloutPercent": 100,
                        }
                    }
                },
                "default": {
                    "strategyId": "global-default-v1",
                    "content": "global default",
                    "enabled": True,
                    "rolloutPercent": 100,
                },
            }
        )

        strategy_library = _load_module(STRATEGY_LIB, "strategy_library_for_test")
        library = strategy_library.load_strategy_library(str(self.root))

        hit_exact = strategy_library.resolve_strategy(library, "coder", "coding", task_id="T-901")
        self.assertEqual(hit_exact["strategyId"], "coding-coder-v1", hit_exact)
        self.assertEqual(hit_exact["matchedBy"], "taskKind+agent", hit_exact)

        hit_task_default = strategy_library.resolve_strategy(library, "debugger", "coding", task_id="T-902")
        self.assertEqual(hit_task_default["strategyId"], "coding-default-v1", hit_task_default)
        self.assertEqual(hit_task_default["matchedBy"], "taskKind default", hit_task_default)

        hit_agent_default = strategy_library.resolve_strategy(library, "coder", "unknown-kind", task_id="T-903")
        self.assertEqual(hit_agent_default["strategyId"], "agent-coder-v1", hit_agent_default)
        self.assertEqual(hit_agent_default["matchedBy"], "agent default", hit_agent_default)

        hit_global_default = strategy_library.resolve_strategy(library, "broadcaster", "unknown-kind", task_id="T-904")
        self.assertEqual(hit_global_default["strategyId"], "global-default-v1", hit_global_default)
        self.assertEqual(hit_global_default["matchedBy"], "global default", hit_global_default)

    def test_rollout_switch_is_stable_and_respects_bounds(self):
        self._write_strategy_config(
            {
                "taskKinds": {
                    "coding": {
                        "coder": {
                            "strategyId": "coding-coder-ab30",
                            "content": "ab 30",
                            "enabled": True,
                            "rolloutPercent": 30,
                        }
                    }
                },
                "agents": {
                    "debugger": {
                        "default": {
                            "strategyId": "debugger-rollout-zero",
                            "content": "disabled by rollout",
                            "enabled": True,
                            "rolloutPercent": 0,
                        }
                    }
                },
                "default": {
                    "strategyId": "global-rollout-full",
                    "content": "always on",
                    "enabled": True,
                    "rolloutPercent": 100,
                },
            }
        )

        strategy_library = _load_module(STRATEGY_LIB, "strategy_library_for_test_rollout")
        library = strategy_library.load_strategy_library(str(self.root))

        first = strategy_library.resolve_strategy(library, "coder", "coding", task_id="T-AB-001")
        second = strategy_library.resolve_strategy(library, "coder", "coding", task_id="T-AB-001")
        self.assertEqual(first["enabled"], second["enabled"], {"first": first, "second": second})

        zero = strategy_library.resolve_strategy(library, "debugger", "unknown-kind", task_id="T-AB-002")
        self.assertFalse(zero["enabled"], zero)

        full = strategy_library.resolve_strategy(library, "broadcaster", "unknown-kind", task_id="T-AB-003")
        self.assertTrue(full["enabled"], full)

    def test_build_agent_prompt_injects_strategy_block(self):
        self._write_strategy_config(
            {
                "taskKinds": {
                    "coding": {
                        "coder": {
                            "strategyId": "coding-coder-inject-v1",
                            "content": "先给最小可交付补丁，再给验证证据。",
                            "enabled": True,
                            "rolloutPercent": 100,
                        }
                    }
                },
                "default": {
                    "strategyId": "global-default-v1",
                    "content": "global",
                    "enabled": True,
                    "rolloutPercent": 100,
                },
            }
        )

        milestones = _load_module(MILE, "milestones_module_for_strategy_prompt_test")
        task = {
            "taskId": "T-950",
            "title": "实现策略注入",
            "status": "pending",
            "owner": "orchestrator",
        }

        prompt = milestones.build_agent_prompt(str(self.root), task, "coder", "T-950: 实现策略注入")

        self.assertIn("ROLE_STRATEGY", prompt, prompt)
        self.assertIn("coding-coder-inject-v1", prompt, prompt)
        self.assertIn("先给最小可交付补丁", prompt, prompt)

    def test_dispatch_output_contains_strategy_mapping(self):
        self._write_strategy_config(
            {
                "taskKinds": {
                    "coding": {
                        "coder": {
                            "strategyId": "coding-coder-dispatch-v1",
                            "content": "先交最小补丁，再给验证证据。",
                            "enabled": True,
                            "rolloutPercent": 100,
                        }
                    }
                }
            }
        )
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)
        self._run_json(
            [
                "python3",
                str(BOARD),
                "apply",
                "--root",
                str(self.root),
                "--actor",
                "orchestrator",
                "--text",
                "@coder create task T-960: 覆盖 dispatch 策略映射",
            ]
        )
        out = self._run_json(
            [
                "python3",
                str(MILE),
                "dispatch",
                "--root",
                str(self.root),
                "--task-id",
                "T-960",
                "--agent",
                "coder",
                "--mode",
                "dry-run",
            ]
        )
        self.assertEqual(out.get("strategyId"), "coding-coder-dispatch-v1", out)
        strategy = out.get("strategy") if isinstance(out.get("strategy"), dict) else {}
        self.assertEqual(strategy.get("matchedBy"), "taskKind+agent", out)
        self.assertTrue(out.get("agentPrompt", "").count("coding-coder-dispatch-v1") >= 1, out)


if __name__ == "__main__":
    unittest.main()
