import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CLASSIFIER = SCRIPTS / "lib" / "failure_classifier.py"


def load_failure_classifier_module():
    spec = importlib.util.spec_from_file_location("failure_classifier_module_for_test", str(CLASSIFIER))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load failure_classifier module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FailureClassifierTests(unittest.TestCase):
    def setUp(self):
        self.module = load_failure_classifier_module()

    def test_classify_context_overflow_from_executor_output(self):
        out = self.module.classify_failure(
            "spawn_failed",
            detail="worker stopped: context length exceeded",
            output_text="Error: maximum context length exceeded for model",
            executor="codex_cli",
        )
        self.assertEqual(out.get("failureType"), "context_overflow", out)
        self.assertEqual(out.get("normalizedReason"), "context_length_exceeded", out)
        self.assertEqual(out.get("recoveryStrategy"), "retry_same_assignee_shrink_scope", out)
        self.assertIn("context_length", out.get("signals") or [], out)

    def test_classify_wrong_direction_from_blocked_text(self):
        out = self.module.classify_failure(
            "blocked_signal",
            detail="not what the task asked: wrote docs instead of tests",
            output_text="This solution does not match the requested deliverable.",
            executor="claude_code",
        )
        self.assertEqual(out.get("failureType"), "wrong_direction", out)
        self.assertEqual(out.get("normalizedReason"), "misaligned_deliverable", out)
        self.assertEqual(out.get("recoveryStrategy"), "escalate_for_replan", out)

    def test_classify_missing_info_from_upstream_dependency(self):
        out = self.module.classify_failure(
            "blocked_signal",
            detail="blocked waiting for upstream SECRET_KEY and API schema",
            output_text="Need the missing env var and schema before continuing.",
        )
        self.assertEqual(out.get("failureType"), "missing_info", out)
        self.assertEqual(out.get("normalizedReason"), "missing_upstream_information", out)
        self.assertEqual(out.get("recoveryStrategy"), "retry_with_clarification", out)

    def test_classify_budget_exceeded_from_reason_code(self):
        out = self.module.classify_failure(
            "budget_exceeded",
            detail="token budget exceeded for task",
            output_text="",
        )
        self.assertEqual(out.get("failureType"), "budget_exceeded", out)
        self.assertEqual(out.get("normalizedReason"), "budget_limit_reached", out)
        self.assertEqual(out.get("recoveryStrategy"), "escalate_budget_review", out)

    def test_classify_continuation_stall_from_reason_and_text(self):
        out = self.module.classify_failure(
            "no_completion_signal",
            detail="checkpoint stalled after partial progress; ask orchestrator to continue",
            output_text="No final answer, continue was required but execution stalled midway.",
        )
        self.assertEqual(out.get("failureType"), "continuation_stall", out)
        self.assertEqual(out.get("normalizedReason"), "continuation_stalled", out)
        self.assertEqual(out.get("recoveryStrategy"), "escalate_after_stall", out)

    def test_unknown_fallback_keeps_reason_normalized(self):
        out = self.module.classify_failure("some_new_reason", detail="opaque failure", output_text="")
        self.assertEqual(out.get("failureType"), "unknown", out)
        self.assertEqual(out.get("normalizedReason"), "some_new_reason", out)
        self.assertEqual(out.get("recoveryStrategy"), "manual_triage", out)


if __name__ == "__main__":
    unittest.main()
