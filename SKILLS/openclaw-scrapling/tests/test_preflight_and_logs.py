import pathlib
import importlib.util
import pathlib
import unittest

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRAPE_PATH = SKILL_DIR / 'scrape.py'

spec = importlib.util.spec_from_file_location('scrape_mod', SCRAPE_PATH)
scrape_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrape_mod)


class PreflightAndLogTests(unittest.TestCase):
    def test_filters_stealth_no_challenge_log_after_success(self):
        stderr_text = (
            '[2026-03-08 00:00:00] ERROR: No Cloudflare challenge found.\n'
            '[2026-03-08 00:00:01] INFO: Fetched (200) <GET https://example.com/>\n'
        )
        visible, suppressed = scrape_mod.filter_fetch_logs(
            mode='stealth',
            stderr_text=stderr_text,
            fetch_succeeded=True,
        )
        self.assertNotIn('No Cloudflare challenge found', visible)
        self.assertIn('No Cloudflare challenge found', suppressed)
        self.assertIn('Fetched (200)', visible)

    def test_disables_cloudflare_solving_when_camoufox_missing(self):
        effective = scrape_mod.resolve_solve_cloudflare(
            mode='stealth',
            solve_cloudflare=True,
            which_fn=lambda name: None,
            skill_dir=pathlib.Path('/tmp/nonexistent-skill-dir'),
        )
        self.assertFalse(effective)

    def test_reports_missing_camoufox_as_warning(self):
        warnings = scrape_mod.build_mode_warnings(
            mode='stealth',
            solve_cloudflare=True,
            which_fn=lambda name: None,
            skill_dir=pathlib.Path('/tmp/nonexistent-skill-dir'),
        )
        self.assertTrue(any('camoufox' in warning.lower() for warning in warnings))


if __name__ == '__main__':
    unittest.main()
