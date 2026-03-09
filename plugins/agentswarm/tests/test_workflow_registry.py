"""Tests for the workflow registry functions in milestones.py."""

import json
import os
import sys
import tempfile
import unittest

SCRIPT_LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))
if SCRIPT_LIB_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_LIB_DIR)

import milestones


class TestListWorkflows(unittest.TestCase):
    def test_list_workflows_returns_paper_xhs(self):
        names = milestones.list_workflows()
        self.assertIn("paper-xhs-3min", names)

    def test_list_workflows_excludes_hidden_files(self):
        names = milestones.list_workflows()
        for name in names:
            self.assertFalse(name.startswith("."))


class TestLoadWorkflowConfig(unittest.TestCase):
    def test_load_paper_xhs_3min(self):
        cfg = milestones.load_workflow_config("paper-xhs-3min")
        self.assertEqual(cfg["name"], "paper-xhs-3min")
        self.assertIsInstance(cfg["stages"], list)
        self.assertEqual(len(cfg["stages"]), 16)

    def test_load_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            milestones.load_workflow_config("nonexistent-workflow")

    def test_caching(self):
        cfg1 = milestones.load_workflow_config("paper-xhs-3min")
        cfg2 = milestones.load_workflow_config("paper-xhs-3min")
        self.assertIs(cfg1, cfg2)


class TestGetWorkflowStages(unittest.TestCase):
    def test_stage_ids(self):
        stages = milestones.get_workflow_stages("paper-xhs-3min")
        ids = [s["stageId"] for s in stages]
        self.assertEqual(ids[0], "A0")
        self.assertIn("J", ids)
        self.assertIn("O", ids)


class TestGetWorkflowEnvRequirements(unittest.TestCase):
    def test_returns_list(self):
        reqs = milestones.get_workflow_env_requirements("paper-xhs-3min")
        self.assertIsInstance(reqs, list)
        self.assertTrue(len(reqs) >= 1)
        self.assertTrue(any("conda" in r for r in reqs))


class TestGetWorkflowPlaceholders(unittest.TestCase):
    def test_contains_core_placeholders(self):
        ph = milestones.get_workflow_placeholders("paper-xhs-3min")
        self.assertIn("paper_id", ph)
        self.assertIn("run_dir", ph)
        self.assertIn("upstream_output_dir", ph)


class TestGetWorkflowContextInjectors(unittest.TestCase):
    def test_paper_context_injector(self):
        injectors = milestones.get_workflow_context_injectors("paper-xhs-3min")
        self.assertIn("paperContext", injectors)
        pc = injectors["paperContext"]
        self.assertTrue(pc["enabled"])
        self.assertIn("J", pc["applyToStages"])
        self.assertNotIn("A", pc["applyToStages"])


class TestReadStageTemplate(unittest.TestCase):
    def test_read_existing_template(self):
        template_dir = milestones.get_workflow_template_dir("paper-xhs-3min")
        if os.path.isdir(template_dir) and os.path.isfile(os.path.join(template_dir, "stage-a0-extract.md")):
            text = milestones.read_stage_template(template_dir, "stage-a0-extract.md")
            self.assertTrue(len(text) > 0)

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            milestones.read_stage_template("/tmp/nonexistent", "missing.md")


class TestRenderStagePrompt(unittest.TestCase):
    def test_basic_render(self):
        template = "Paper: {paper_id}, Dir: {run_dir}"
        result = milestones.render_stage_prompt(
            template,
            {"paper_id", "run_dir"},
            {"paper_id": "test-123", "run_dir": "/tmp/out"},
        )
        self.assertEqual(result, "Paper: test-123, Dir: /tmp/out")

    def test_unsupported_placeholder_raises(self):
        template = "Value: {unknown_field}"
        with self.assertRaises(ValueError):
            milestones.render_stage_prompt(template, {"paper_id"}, {})

    def test_empty_template(self):
        result = milestones.render_stage_prompt("", {"paper_id"}, {})
        self.assertEqual(result, "")


class TestNormalizeOutputName(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(milestones.normalize_output_name("Hello World!"), "Hello-World")

    def test_empty(self):
        self.assertEqual(milestones.normalize_output_name(""), "untitled")


class TestDetectWorkflowFromProject(unittest.TestCase):
    def test_detect_by_marker_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = os.path.join(tmpdir, "orchestrator-bootstrap.json")
            with open(marker, "w") as f:
                json.dump({"workflowName": "paper-xhs-3min"}, f)
            result = milestones.detect_workflow_from_project(tmpdir)
            self.assertEqual(result, "paper-xhs-3min")

    def test_detect_by_path_name(self):
        result = milestones.detect_workflow_from_project("/some/path/paper-xhs-3min-workflow/run1")
        self.assertEqual(result, "paper-xhs-3min")

    def test_no_match(self):
        result = milestones.detect_workflow_from_project("/some/random/path")
        self.assertIsNone(result)

    def test_none_input(self):
        result = milestones.detect_workflow_from_project("")
        self.assertIsNone(result)


class TestExtractStageIdFromTitle(unittest.TestCase):
    def test_standard_title(self):
        self.assertEqual(milestones._extract_stage_id_from_title("[paper-xhs-3min:test] Stage J: Reproduction scope"), "J")

    def test_a0(self):
        self.assertEqual(milestones._extract_stage_id_from_title("Stage A0: Extract"), "A0")

    def test_no_match(self):
        self.assertEqual(milestones._extract_stage_id_from_title("Some random title"), "")


class TestLegacyWrappers(unittest.TestCase):
    def test_normalize_xhs_output_name(self):
        self.assertEqual(milestones.normalize_xhs_output_name("hello world"), "hello-world")
        self.assertEqual(milestones.normalize_xhs_output_name(""), "untitled-paper")

    def test_render_xhs_stage_prompt_delegates(self):
        template = "Paper: {paper_id}"
        result = milestones.render_xhs_stage_prompt(template, {"paper_id": "test"})
        self.assertEqual(result, "Paper: test")


if __name__ == "__main__":
    unittest.main()
