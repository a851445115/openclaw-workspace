"""Tests for the paper_context_injector module."""

import os
import sys
import tempfile
import unittest

SCRIPT_LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))
if SCRIPT_LIB_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_LIB_DIR)

import paper_context_injector


class TestLoadPaperContext(unittest.TestCase):
    def test_disabled_injector_returns_none(self):
        cfg = {"enabled": False, "applyToStages": ["J"], "sources": ["x.md"]}
        self.assertIsNone(paper_context_injector.load_paper_context("/tmp", cfg, "J"))

    def test_stage_not_in_apply_list(self):
        cfg = {"enabled": True, "applyToStages": ["J", "K"], "sources": ["x.md"]}
        self.assertIsNone(paper_context_injector.load_paper_context("/tmp", cfg, "A"))

    def test_no_sources(self):
        cfg = {"enabled": True, "applyToStages": ["J"], "sources": []}
        self.assertIsNone(paper_context_injector.load_paper_context("/tmp", cfg, "J"))

    def test_missing_source_file(self):
        cfg = {"enabled": True, "applyToStages": ["J"], "sources": ["/nonexistent/file.md"]}
        self.assertIsNone(paper_context_injector.load_paper_context("/tmp", cfg, "J"))

    def test_reads_source_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "raw-text.md")
            with open(src, "w") as f:
                f.write("Paper content here")
            cfg = {
                "enabled": True,
                "applyToStages": ["J", "K"],
                "sources": [os.path.join("{run_dir}", "raw-text.md")],
            }
            result = paper_context_injector.load_paper_context(tmpdir, cfg, "J")
            self.assertIsNotNone(result)
            self.assertIn("Paper content here", result)

    def test_run_dir_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "test.md")
            with open(src, "w") as f:
                f.write("content")
            cfg = {
                "enabled": True,
                "applyToStages": ["L"],
                "sources": ["{run_dir}/test.md"],
            }
            result = paper_context_injector.load_paper_context(tmpdir, cfg, "L")
            self.assertEqual(result, "content")


class TestInjectIntoPrompt(unittest.TestCase):
    def test_no_injectors(self):
        prompt = "original prompt"
        result = paper_context_injector.inject_into_prompt(prompt, "/tmp", {}, "J")
        self.assertEqual(result, prompt)

    def test_inject_paper_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "data.md")
            with open(src, "w") as f:
                f.write("injected data")
            injectors = {
                "paperContext": {
                    "enabled": True,
                    "applyToStages": ["J"],
                    "sources": ["{run_dir}/data.md"],
                }
            }
            result = paper_context_injector.inject_into_prompt("base prompt", tmpdir, injectors, "J")
            self.assertIn("base prompt", result)
            self.assertIn("INJECTED_CONTEXT (paperContext):", result)
            self.assertIn("injected data", result)

    def test_no_injection_for_wrong_stage(self):
        injectors = {
            "paperContext": {
                "enabled": True,
                "applyToStages": ["J"],
                "sources": ["/nonexistent.md"],
            }
        }
        result = paper_context_injector.inject_into_prompt("base", "/tmp", injectors, "A")
        self.assertEqual(result, "base")


class TestMaxContextChars(unittest.TestCase):
    def test_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "big.md")
            with open(src, "w") as f:
                f.write("x" * 20000)
            cfg = {
                "enabled": True,
                "applyToStages": ["J"],
                "sources": ["{run_dir}/big.md"],
            }
            result = paper_context_injector.load_paper_context(tmpdir, cfg, "J")
            self.assertIsNotNone(result)
            self.assertLessEqual(len(result), paper_context_injector.MAX_CONTEXT_CHARS)


if __name__ == "__main__":
    unittest.main()
