import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
COLLAB_HUB = REPO / "scripts" / "lib" / "collaboration_hub.py"


def load_collaboration_hub_module():
    spec = importlib.util.spec_from_file_location("collaboration_hub_module_for_test", str(COLLAB_HUB))
    if spec is None or spec.loader is None:
        raise AssertionError("failed to load collaboration_hub module for tests")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CollaborationHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mod = load_collaboration_hub_module()

    def tearDown(self):
        self.tmp.cleanup()

    def _message(self, message_type: str = "question", summary: str = "Need guidance"):
        return {
            "taskId": "T-COLLAB-1",
            "threadId": "TH-1",
            "fromAgent": "coder",
            "toAgent": "debugger",
            "messageType": message_type,
            "summary": summary,
            "evidence": ["logs/run.log"],
            "request": "Please help validate root cause",
            "deadline": "2026-03-05T12:00:00Z",
            "createdAt": "2026-03-04T09:30:00Z",
        }

    def test_validate_message_accepts_valid_payload(self):
        valid = self._message("handoff")

        result = self.mod.validate_message(valid)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("errors"), [], result)

    def test_validate_message_rejects_invalid_payload(self):
        invalid = self._message("unexpected-type")
        invalid.pop("summary")
        invalid["evidence"] = "not-a-list"

        result = self.mod.validate_message(invalid)

        self.assertFalse(result.get("ok"), result)
        joined = "\n".join(result.get("errors") or [])
        self.assertIn("summary", joined, result)
        self.assertIn("messageType", joined, result)
        self.assertIn("evidence", joined, result)

    def test_append_message_updates_thread_summary(self):
        first = self.mod.append_message(self.root.as_posix(), self._message("question", "Where is failure?"))
        second_payload = self._message("answer", "Root cause is timeout")
        second_payload["fromAgent"] = "debugger"
        second_payload["toAgent"] = "coder"
        second = self.mod.append_message(self.root.as_posix(), second_payload)

        thread = self.mod.get_thread(self.root.as_posix(), "TH-1")
        messages = self.mod.list_thread_messages(self.root.as_posix(), "TH-1")
        summary = self.mod.summarize_thread(self.root.as_posix(), "TH-1")

        self.assertTrue(first.get("ok"), first)
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(len(messages), 2, messages)
        self.assertEqual(thread.get("status"), "active", thread)
        self.assertEqual(thread.get("rounds"), 1, thread)
        participants = thread.get("participants") or []
        self.assertIn("coder", participants, thread)
        self.assertIn("debugger", participants, thread)
        self.assertEqual(summary.get("messageCount"), 2, summary)
        self.assertEqual(summary.get("threadId"), "TH-1", summary)

    def test_question_and_consult_use_dedupe_key(self):
        first = self.mod.append_message(
            self.root.as_posix(),
            self._message("question", "How should we retry this request?"),
        )
        duplicate_question = self._message("question", "  How should we   retry this request? ")
        duplicate = self.mod.append_message(self.root.as_posix(), duplicate_question)
        consult_variant = self._message("consult", "How should we retry this request?")
        consult = self.mod.append_message(self.root.as_posix(), consult_variant)

        messages = self.mod.list_thread_messages(self.root.as_posix(), "TH-1")

        self.assertTrue(first.get("ok"), first)
        self.assertFalse(duplicate.get("ok"), duplicate)
        self.assertEqual(duplicate.get("reason"), "duplicate_question", duplicate)
        self.assertFalse(consult.get("ok"), consult)
        self.assertEqual(consult.get("reason"), "duplicate_question", consult)
        self.assertEqual(len(messages), 1, messages)

    def test_round_limit_trigger(self):
        policy = {"maxRoundsPerThread": 3}
        for index in range(3):
            payload = self._message("question", f"question-{index}")
            payload["request"] = f"request-{index}"
            payload["createdAt"] = f"2026-03-04T09:3{index}:00Z"
            out = self.mod.append_message(self.root.as_posix(), payload, policy=policy)
            self.assertTrue(out.get("ok"), out)

        thread = self.mod.get_thread(self.root.as_posix(), "TH-1")
        self.assertEqual(thread.get("rounds"), 3, thread)
        self.assertTrue(self.mod.should_escalate_round_limit(thread, policy.get("maxRoundsPerThread")), thread)

    def test_corrupted_or_empty_files_are_tolerated(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "collab.threads.json").write_text("{broken", encoding="utf-8")
        (state_dir / "collab.messages.jsonl").write_text("not-json\n", encoding="utf-8")

        thread = self.mod.get_thread(self.root.as_posix(), "TH-missing")
        messages = self.mod.list_thread_messages(self.root.as_posix(), "TH-missing")
        summary = self.mod.summarize_thread(self.root.as_posix(), "TH-missing")

        self.assertEqual(thread, {}, thread)
        self.assertEqual(messages, [], messages)
        self.assertEqual(summary.get("threadId"), "TH-missing", summary)
        self.assertEqual(summary.get("messageCount"), 0, summary)

        appended = self.mod.append_message(self.root.as_posix(), self._message("decision", "Proceed"))
        self.assertTrue(appended.get("ok"), appended)

        raw_threads = json.loads((state_dir / "collab.threads.json").read_text(encoding="utf-8"))
        self.assertIn("threads", raw_threads, raw_threads)

    def test_should_escalate_timeout_uses_latest_activity(self):
        thread = {"lastMessageAt": "2026-03-04T09:00:00Z"}
        self.assertFalse(
            self.mod.should_escalate_timeout(thread, timeout_minutes=30, now_iso_value="2026-03-04T09:20:00Z")
        )
        self.assertTrue(
            self.mod.should_escalate_timeout(thread, timeout_minutes=30, now_iso_value="2026-03-04T09:31:00Z")
        )


if __name__ == "__main__":
    unittest.main()
