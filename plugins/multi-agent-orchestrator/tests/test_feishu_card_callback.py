import importlib.machinery
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
INIT = SCRIPTS / "init-task-board"
INBOUND = SCRIPTS / "feishu-inbound-router"
FIXTURE = REPO / "tests" / "fixtures" / "feishu_card_callback_wrapper.txt"


def run_inbound(root: Path, raw: str):
    proc = subprocess.run(
        [
            "python3",
            str(INBOUND),
            "--root",
            str(root),
            "--milestones",
            "dry-run",
        ],
        cwd=REPO,
        input=raw,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"inbound failed (rc={proc.returncode})\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    try:
        return json.loads(proc.stdout.strip())
    except Exception as err:
        raise AssertionError(f"invalid inbound json: {err}\nstdout={proc.stdout}\nstderr={proc.stderr}")


def load_inbound_module():
    loader = importlib.machinery.SourceFileLoader("feishu_inbound_router_for_test", str(INBOUND))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError("failed to create module spec for feishu-inbound-router")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_wrapper(
    text: str,
    conv: Dict[str, object],
    sender: Dict[str, object],
    card_callback: Optional[Dict[str, object]] = None,
    extra_json_sections: Optional[List[Tuple[str, Dict[str, object]]]] = None,
):
    lines = [
        f"message in group oc_test_group: {text}",
        "",
        "Conversation info (untrusted metadata):",
        "```json",
        json.dumps(conv, ensure_ascii=True, indent=2),
        "```",
        "",
        "Sender (untrusted metadata):",
        "```json",
        json.dumps(sender, ensure_ascii=True, indent=2),
        "```",
        "",
    ]
    if card_callback is not None:
        lines.extend(
            [
                "Card callback (untrusted metadata):",
                "```json",
                json.dumps(card_callback, ensure_ascii=True, indent=2),
                "```",
                "",
            ]
        )
    if extra_json_sections:
        for title, obj in extra_json_sections:
            lines.extend(
                [
                    title,
                    "```json",
                    json.dumps(obj, ensure_ascii=True, indent=2),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines)


def make_card_callback_wrapper(
    command: str,
    message_id: str = "om_test_msg_001",
    action_ts: str = "1700000000",
    sender_name: str = "Test User",
    sender_open_id: str = "ou_test_user_001",
):
    conv = {
        "conversation_label": "oc_test_group",
        "sender": sender_name,
        "message_id": message_id,
        "was_mentioned": True,
    }
    sender = {
        "name": sender_name,
        "sender_type": "user",
        "open_id": sender_open_id,
    }
    callback = {
        "open_message_id": message_id,
        "action": {
            "tag": "button",
            "value": {
                "command": command,
                "message_id": message_id,
                "action_ts": action_ts,
            },
        },
    }
    return make_wrapper(
        text="@orchestrator status",
        conv=conv,
        sender=sender,
        card_callback=callback,
    )


def make_text_wrapper(text: str):
    return make_wrapper(
        text=text,
        conv={
            "conversation_label": "oc_test_group",
            "sender": "tester",
            "was_mentioned": True,
        },
        sender={
            "name": "Test User",
            "sender_type": "user",
        },
    )


class FeishuCardCallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run([str(INIT), "--root", str(self.root)], cwd=REPO, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_card_callback_routes_action_value_command_and_sets_source_ack(self):
        raw = make_card_callback_wrapper("@orchestrator create task: callback route")
        out = run_inbound(self.root, raw)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("text"), "@orchestrator create task: callback route", out)
        self.assertEqual((out.get("router") or {}).get("intent"), "board_cmd", out)
        self.assertEqual((out.get("router") or {}).get("source"), "card_callback", out)
        ack = (out.get("router") or {}).get("ack") or {}
        self.assertTrue(ack.get("ok"), out)

    def test_card_callback_dedup_ignores_repeated_click(self):
        raw = make_card_callback_wrapper(
            "@orchestrator create task: callback dedup",
            message_id="om_test_msg_dedup",
            action_ts="1700000010",
        )
        first = run_inbound(self.root, raw)
        second = run_inbound(self.root, raw)

        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertEqual(first.get("text"), "@orchestrator create task: callback dedup", first)
        self.assertFalse(bool(first.get("deduplicated")), first)
        self.assertTrue(bool(second.get("deduplicated")), second)
        self.assertEqual((second.get("router") or {}).get("source"), "card_callback", second)
        self.assertEqual((second.get("router") or {}).get("intent"), "deduplicated_callback", second)

    def test_text_fallback_keeps_existing_command_path(self):
        raw = make_text_wrapper("@orchestrator create task: text fallback")
        out = run_inbound(self.root, raw)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("text"), "@orchestrator create task: text fallback", out)
        self.assertEqual((out.get("router") or {}).get("intent"), "board_cmd", out)
        self.assertNotEqual((out.get("router") or {}).get("source"), "card_callback", out)

    def test_json_in_message_body_does_not_trigger_card_callback(self):
        raw = make_wrapper(
            text='@orchestrator create task: body json no callback {"sample": true}',
            conv={
                "conversation_label": "oc_test_group",
                "sender": "tester",
                "was_mentioned": True,
            },
            sender={
                "name": "Test User",
                "sender_type": "user",
            },
            card_callback=None,
            extra_json_sections=[
                (
                    "Body JSON sample (not callback metadata):",
                    {
                        "action": {
                            "value": {
                                "command": "@orchestrator create task: should-not-run-from-body-json"
                            }
                        }
                    },
                )
            ],
        )
        out = run_inbound(self.root, raw)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("text"), '@orchestrator create task: body json no callback {"sample": true}', out)
        self.assertEqual((out.get("router") or {}).get("intent"), "board_cmd", out)
        self.assertNotEqual(out.get("source"), "card_callback", out)
        self.assertNotEqual((out.get("router") or {}).get("source"), "card_callback", out)

    def test_callback_field_priority_prefers_action_over_conversation_fields(self):
        raw = make_wrapper(
            text="@orchestrator status",
            conv={
                "conversation_label": "oc_test_group",
                "sender": "tester",
                "message_id": "conv_message_id",
                "action_ts": "conv_action_ts",
                "was_mentioned": True,
            },
            sender={
                "name": "Test User",
                "sender_type": "user",
                "open_id": "ou_priority_user",
            },
            card_callback={
                "message_id": "callback_message_id",
                "action_ts": "callback_action_ts",
                "action": {
                    "value": {
                        "command": "@orchestrator create task: field priority",
                        "message_id": "action_message_id",
                        "action_ts": "action_ts_123",
                    }
                },
            },
        )
        out = run_inbound(self.root, raw)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out.get("text"), "@orchestrator create task: field priority", out)
        ack = (out.get("router") or {}).get("ack") or {}
        self.assertEqual(ack.get("messageId"), "action_message_id", out)
        self.assertEqual(ack.get("actionTs"), "action_ts_123", out)

    def test_callback_dedup_isolated_by_sender_identity(self):
        first = make_card_callback_wrapper(
            "@orchestrator create task: sender one",
            message_id="om_user_scope_1",
            action_ts="1700010101",
            sender_name="User One",
            sender_open_id="ou_sender_one",
        )
        second = make_card_callback_wrapper(
            "@orchestrator create task: sender one",
            message_id="om_user_scope_1",
            action_ts="1700010101",
            sender_name="User Two",
            sender_open_id="ou_sender_two",
        )

        first_out = run_inbound(self.root, first)
        second_out = run_inbound(self.root, second)
        self.assertTrue(first_out["ok"], first_out)
        self.assertTrue(second_out["ok"], second_out)
        self.assertFalse(bool(first_out.get("deduplicated")), first_out)
        self.assertFalse(bool(second_out.get("deduplicated")), second_out)

    def test_callback_missing_message_and_action_ts_still_has_ttl_dedup_protection(self):
        raw = make_wrapper(
            text="@orchestrator status",
            conv={
                "conversation_label": "oc_test_group",
                "sender": "tester",
                "was_mentioned": True,
            },
            sender={
                "name": "NoTs User",
                "sender_type": "user",
                "open_id": "ou_no_ts_user",
            },
            card_callback={
                "event_id": "evt_no_ts_001",
                "action": {
                    "value": {
                        "command": "@orchestrator create task: missing ids",
                    }
                },
            },
        )
        first = run_inbound(self.root, raw)
        second = run_inbound(self.root, raw)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertFalse(bool(first.get("deduplicated")), first)
        self.assertTrue(bool(second.get("deduplicated")), second)

    def test_save_callback_dedup_state_replace_conflict_does_not_crash(self):
        module = load_inbound_module()
        state = {"entries": {"k1": {"seenAtTs": 1, "ttlSec": 60}}}
        original_replace = module.os.replace
        calls = {"count": 0}

        def flaky_replace(src, dst):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FileNotFoundError("simulated tmp race")
            return original_replace(src, dst)

        with patch.object(module.os, "replace", side_effect=flaky_replace):
            module.save_callback_dedup_state(str(self.root), state)

        self.assertGreaterEqual(calls["count"], 2)
        loaded = module.load_callback_dedup_state(str(self.root))
        self.assertIn("k1", loaded.get("entries") or {}, loaded)

    def test_dirty_dedup_entries_fail_open_and_flow_continues(self):
        state_file = self.root / "state" / "feishu-card-callback-dedup.json"
        state_file.write_text(
            json.dumps(
                {
                    "entries": {
                        "dirty-1": {"seenAtTs": "not-an-int", "ttlSec": "x"},
                        "dirty-2": {"seenAtTs": {"nested": 1}, "ttlSec": []},
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        raw = make_card_callback_wrapper(
            "@orchestrator create task: dirty-state-safe",
            message_id="om_dirty_state_safe",
            action_ts="1700099999",
            sender_name="Dirty Safe User",
            sender_open_id="ou_dirty_safe",
        )
        first = run_inbound(self.root, raw)
        second = run_inbound(self.root, raw)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertFalse(bool(first.get("deduplicated")), first)
        self.assertTrue(bool(second.get("deduplicated")), second)


if __name__ == "__main__":
    unittest.main()
