import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "lib" / "multi_reviewer.py"


def load_module():
    spec = importlib.util.spec_from_file_location("multi_reviewer_module_for_tests", str(MODULE_PATH))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load multi_reviewer module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MultiReviewerTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_normalize_reviewer_policy_uses_default_reviewers(self):
        policy = self.mod.normalize_reviewer_policy({})
        reviewers = policy.get("reviewers") or []
        models = [str(item.get("model")) for item in reviewers]
        self.assertEqual(models, ["codex", "claude", "gemini"], policy)
        total_weight = sum(float(item.get("weight") or 0.0) for item in reviewers)
        self.assertAlmostEqual(total_weight, 1.0, places=6, msg=str(policy))

    def test_aggregate_review_scores_returns_weighted_total(self):
        policy = self.mod.normalize_reviewer_policy(
            {
                "reviewers": [
                    {"model": "codex", "weight": 0.5},
                    {"model": "claude", "weight": 0.3},
                    {"model": "gemini", "weight": 0.2},
                ]
            }
        )
        agg = self.mod.aggregate_review_scores(
            policy,
            {
                "codex": {"score": 0.8},
                "claude": {"score": 0.6},
                "gemini": {"score": 0.5},
            },
        )
        self.assertAlmostEqual(float(agg.get("totalScore") or 0.0), 0.68, places=6, msg=str(agg))
        self.assertEqual(len(agg.get("breakdown") or []), 3, agg)

    def test_decide_review_pass_respects_degraded_guard(self):
        policy = self.mod.normalize_reviewer_policy({"passThreshold": 0.7, "allowDegradedPass": False})
        passed = self.mod.decide_review_pass(total_score=0.8, policy=policy, degraded=False)
        self.assertTrue(passed.get("pass"), passed)
        blocked = self.mod.decide_review_pass(total_score=0.8, policy=policy, degraded=True)
        self.assertFalse(blocked.get("pass"), blocked)

    def test_run_multi_review_with_runner_collects_outputs(self):
        def runner(reviewer, changes, context):
            _ = (changes, context)
            scores = {"codex": 0.9, "claude": 0.8, "gemini": 0.7}
            return {"score": scores.get(str(reviewer.get("model")), 0.0), "notes": "ok"}

        out = self.mod.run_multi_review(changes="dummy diff", runner=runner, policy={"enabled": True, "dryRun": False})
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("degraded"), out)
        self.assertEqual(len(out.get("breakdown") or []), 3, out)
        self.assertGreater(float(out.get("totalScore") or 0.0), 0.0, out)

    def test_run_multi_review_without_runner_degrades_gracefully(self):
        out = self.mod.run_multi_review(
            changes="dummy diff",
            runner=None,
            policy={"enabled": True, "dryRun": False, "allowDegradedPass": True},
        )
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("degraded"), out)
        self.assertEqual(str(out.get("reason")), "runner_unavailable", out)
        conclusion = out.get("conclusion") or {}
        self.assertTrue(conclusion.get("pass"), out)


if __name__ == "__main__":
    unittest.main()
