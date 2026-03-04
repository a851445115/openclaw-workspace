import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DECOMP = REPO / "scripts" / "lib" / "task_decomposer.py"


def load_task_decomposer_module():
    spec = importlib.util.spec_from_file_location("task_decomposer_module_for_test", str(DECOMP))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load task_decomposer module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TaskDecompositionTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_task_decomposer_module()

    def test_decompose_project_outputs_unique_titles_owner_hint_and_dep_chain(self):
        doc_text = "\n".join(
            [
                "# 智能报告项目",
                "## 目标",
                "- 构建数据采集与清洗流水线",
                "- 构建数据采集与清洗流水线",
                "## 14. 里程碑建议",
                "- M1：实现数据模型与指标定义",
                "- M2：实现调度任务与自动重试",
                "- M3：编写验收测试与发布说明",
            ]
        )
        tasks = self.mod.decompose_project("/tmp/demo-project", "智能报告项目", doc_text)
        self.assertGreaterEqual(len(tasks), 3, tasks)

        normalized = []
        for task in tasks:
            title = str(task.get("title") or "").strip()
            self.assertTrue(title, task)
            normalized.append(self.mod.normalize_task_title(title))
            self.assertTrue(str(task.get("ownerHint") or "").strip(), task)

        self.assertEqual(len(normalized), len(set(normalized)), tasks)
        self.assertTrue(any((task.get("dependsOn") or []) for task in tasks[1:]), tasks)

    def test_policy_max_tasks_limit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "config" / "decomposition-policy.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                json.dumps(
                    {
                        "maxTasks": 2,
                        "minConfidence": 0.0,
                        "requireHumanConfirm": False,
                        "ownerRules": {"测试": "debugger", "发布": "broadcaster"},
                    },
                    ensure_ascii=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            doc_text = "\n".join(
                [
                    "# Demo",
                    "- M1: 编写测试计划",
                    "- M2: 发布上线说明",
                    "- M3: 修复异常回归",
                ]
            )
            tasks = self.mod.decompose_project(str(root), "Demo", doc_text)
            self.assertEqual(len(tasks), 2, tasks)


if __name__ == "__main__":
    unittest.main()
