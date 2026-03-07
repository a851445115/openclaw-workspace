import importlib.util
import os
import pathlib
import tempfile
import unittest

os.environ['OPENCLAW_SCRAPLING_SKIP_VENV_REEXEC'] = '1'

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRAPE_PATH = SKILL_DIR / 'scrape.py'

spec = importlib.util.spec_from_file_location('scrape_mod', SCRAPE_PATH)
scrape_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrape_mod)


class RuntimeEnvTests(unittest.TestCase):
    def test_prefers_local_skill_venv_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = pathlib.Path(tmp)
            py = skill_dir / '.venv' / 'bin' / 'python'
            py.parent.mkdir(parents=True)
            py.write_text('#!python')
            resolved = scrape_mod.preferred_venv_python(skill_dir)
            self.assertEqual(resolved.resolve(), py.resolve())

    def test_finds_runtime_binary_inside_skill_venv_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = pathlib.Path(tmp)
            camoufox = skill_dir / '.venv' / 'bin' / 'camoufox'
            camoufox.parent.mkdir(parents=True)
            camoufox.write_text('#!/bin/sh')
            resolved = scrape_mod.find_runtime_binary('camoufox', skill_dir=skill_dir, which_fn=lambda _name: None)
            self.assertEqual(resolved.resolve(), camoufox.resolve())

    def test_returns_none_when_skill_venv_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = pathlib.Path(tmp)
            resolved = scrape_mod.preferred_venv_python(skill_dir)
            self.assertIsNone(resolved)


if __name__ == '__main__':
    unittest.main()
