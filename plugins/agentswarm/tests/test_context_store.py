import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import importlib.util

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CTX = SCRIPTS / "lib" / "context_store.py"
CLI = SCRIPTS / "context-store"


def load_context_store_module():
    spec = importlib.util.spec_from_file_location("context_store_module_for_test", str(CTX))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load context_store module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_json(cmd, cwd=REPO):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid json output: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'state').mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_context_store_module_initializes_schema(self):
        module = load_context_store_module()
        db_path = module.context_store_path(str(self.root))
        self.assertFalse(Path(db_path).exists())
        init_out = module.init_schema(str(self.root))
        self.assertTrue(init_out.get('ok'), init_out)
        self.assertTrue(Path(db_path).exists(), db_path)

    def test_context_store_cli_round_trip(self):
        run_json(["python3", str(CLI), "init", "--root", str(self.root)])
        put_customer = run_json([
            "python3", str(CLI), "put-customer", "--root", str(self.root),
            "--id", "cust-a", "--name", "ACME", "--requirements", "Need weekly reports", "--tech-stack", "Python,SQLite"
        ])
        self.assertTrue(put_customer.get('ok'), put_customer)
        got_customer = run_json(["python3", str(CLI), "get-customer", "--root", str(self.root), "--id", "cust-a"])
        self.assertEqual((got_customer.get('customer') or {}).get('name'), 'ACME', got_customer)

        put_paper = run_json([
            "python3", str(CLI), "put-paper", "--root", str(self.root),
            "--id", "paper-a", "--title", "Test Paper", "--authors", "Alice;Bob", "--arxiv-id", "2501.00001", "--difficulty-score", "0.7"
        ])
        self.assertTrue(put_paper.get('ok'), put_paper)
        got_paper = run_json(["python3", str(CLI), "get-paper", "--root", str(self.root), "--id", "paper-a"])
        self.assertEqual((got_paper.get('paper') or {}).get('title'), 'Test Paper', got_paper)

        add_history = run_json([
            "python3", str(CLI), "add-history", "--root", str(self.root),
            "--paper-id", "paper-a", "--success", "true", "--issues", "none", "--lessons-learned", "use smaller batch"
        ])
        self.assertTrue(add_history.get('ok'), add_history)
        history = run_json(["python3", str(CLI), "list-history", "--root", str(self.root), "--paper-id", "paper-a"])
        rows = history.get('items') or []
        self.assertEqual(len(rows), 1, history)
        self.assertEqual(rows[0].get('lessonsLearned'), 'use smaller batch', history)
