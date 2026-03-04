import importlib.util
import json
import multiprocessing
import tempfile
import unittest
from unittest import mock
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


def _multiprocess_append_worker(root: str, payload: dict, start_event, result_queue):
    module = load_collaboration_hub_module()
    start_event.wait(timeout=5)
    try:
        result_queue.put(module.append_message(root, payload))
    except Exception as exc:  # pragma: no cover - defensive for subprocess transport
        result_queue.put({"ok": False, "exception": repr(exc)})


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

    def _read_messages_file(self):
        path = self.root / "state" / "collab.messages.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            token = line.strip()
            if not token:
                continue
            rows.append(json.loads(token))
        return rows

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

    def test_dedupe_scope_can_be_configured(self):
        baseline = self._message("question", "How should we retry this request?")
        baseline["request"] = "How should we retry this request?"
        first = self.mod.append_message(self.root.as_posix(), baseline)

        cross_thread = self._message("question", "How should we retry this request?")
        cross_thread["threadId"] = "TH-2"
        cross_thread["request"] = "How should we retry this request?"
        duplicate = self.mod.append_message(self.root.as_posix(), cross_thread)

        scoped_root = self.root / "scope-task-thread"
        scoped_root.mkdir(parents=True, exist_ok=True)
        scoped_first = self.mod.append_message(
            scoped_root.as_posix(),
            baseline,
            policy={"questionDedupeScope": "task_thread"},
        )
        scoped_second = self.mod.append_message(
            scoped_root.as_posix(),
            cross_thread,
            policy={"questionDedupeScope": "task_thread"},
        )

        self.assertTrue(first.get("ok"), first)
        self.assertFalse(duplicate.get("ok"), duplicate)
        self.assertEqual(duplicate.get("reason"), "duplicate_question", duplicate)
        self.assertTrue(scoped_first.get("ok"), scoped_first)
        self.assertTrue(scoped_second.get("ok"), scoped_second)

    def test_multiprocess_same_key_allows_only_one_append(self):
        message = self._message("question", "How should we retry this request?")
        message["request"] = "How should we retry this request?"

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        workers = [
            ctx.Process(
                target=_multiprocess_append_worker,
                args=(self.root.as_posix(), message, start_event, result_queue),
            )
            for _ in range(2)
        ]

        for worker in workers:
            worker.start()
        start_event.set()
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        outputs = [result_queue.get(timeout=5) for _ in workers]
        success_count = sum(1 for item in outputs if item.get("ok"))
        duplicate_count = sum(1 for item in outputs if item.get("reason") == "duplicate_question")
        self.assertEqual(success_count, 1, outputs)
        self.assertEqual(duplicate_count, 1, outputs)
        self.assertEqual(len(self._read_messages_file()), 1)

    def test_multiprocess_different_keys_preserve_all_messages(self):
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        workers = []
        expected = 6
        for index in range(expected):
            payload = self._message("question", f"Need guidance-{index}")
            payload["threadId"] = f"TH-{index}"
            payload["request"] = f"request-{index}"
            payload["createdAt"] = f"2026-03-04T09:{30 + index:02d}:00Z"
            workers.append(
                ctx.Process(
                    target=_multiprocess_append_worker,
                    args=(self.root.as_posix(), payload, start_event, result_queue),
                )
            )

        for worker in workers:
            worker.start()
        start_event.set()
        for worker in workers:
            worker.join(timeout=10)
            self.assertEqual(worker.exitcode, 0)

        outputs = [result_queue.get(timeout=5) for _ in workers]
        failed = [item for item in outputs if not item.get("ok")]
        self.assertEqual(failed, [], outputs)

        messages = self._read_messages_file()
        self.assertEqual(len(messages), expected, messages)
        for index in range(expected):
            thread = self.mod.get_thread(self.root.as_posix(), f"TH-{index}")
            self.assertEqual(thread.get("messageCount"), 1, thread)

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

    def test_corrupted_threads_file_blocks_append_fail_closed(self):
        state_dir = self.root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "collab.threads.json").write_text("{broken", encoding="utf-8")
        (state_dir / "collab.messages.jsonl").write_text("{\"threadId\":\"TH-1\"}\n", encoding="utf-8")

        thread = self.mod.get_thread(self.root.as_posix(), "TH-missing")
        messages = self.mod.list_thread_messages(self.root.as_posix(), "TH-missing")
        summary = self.mod.summarize_thread(self.root.as_posix(), "TH-missing")

        self.assertEqual(thread, {}, thread)
        self.assertEqual(messages, [], messages)
        self.assertEqual(summary.get("threadId"), "TH-missing", summary)
        self.assertEqual(summary.get("messageCount"), 0, summary)

        before_threads = (state_dir / "collab.threads.json").read_text(encoding="utf-8")
        before_messages = (state_dir / "collab.messages.jsonl").read_text(encoding="utf-8")

        appended = self.mod.append_message(self.root.as_posix(), self._message("decision", "Proceed"))
        self.assertFalse(appended.get("ok"), appended)
        self.assertEqual(appended.get("reason"), "threads_state_unreadable", appended)

        self.assertEqual((state_dir / "collab.threads.json").read_text(encoding="utf-8"), before_threads)
        self.assertEqual((state_dir / "collab.messages.jsonl").read_text(encoding="utf-8"), before_messages)

    def test_failed_second_file_write_is_recovered_next_append(self):
        original = self.mod._save_threads_state

        call_count = {"count": 0}

        def flaky_save(root, state):
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise OSError("simulated thread write failure")
            return original(root, state)

        with mock.patch.object(self.mod, "_save_threads_state", side_effect=flaky_save):
            with self.assertRaises(OSError):
                self.mod.append_message(self.root.as_posix(), self._message("question", "Where is failure?"))

        txn_path = self.root / "state" / "collab.append.transaction.json"
        self.assertTrue(txn_path.exists(), txn_path)
        self.assertEqual(len(self._read_messages_file()), 1)

        second = self._message("answer", "Root cause is timeout")
        second["fromAgent"] = "debugger"
        second["toAgent"] = "coder"
        second["request"] = "Resolved"
        second["createdAt"] = "2026-03-04T09:31:00Z"
        recovered = self.mod.append_message(self.root.as_posix(), second)
        self.assertTrue(recovered.get("ok"), recovered)
        self.assertEqual(recovered.get("recovery"), "applied_pending_transaction", recovered)

        thread = self.mod.get_thread(self.root.as_posix(), "TH-1")
        messages = self.mod.list_thread_messages(self.root.as_posix(), "TH-1")
        self.assertEqual(len(messages), 2, messages)
        self.assertEqual(thread.get("messageCount"), 2, thread)
        self.assertFalse(txn_path.exists())

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
