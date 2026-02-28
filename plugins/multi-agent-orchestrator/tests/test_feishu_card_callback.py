import json
import subprocess
import tempfile
import unittest
from pathlib import Path


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


def make_card_callback_wrapper(command: str, message_id: str = "om_test_msg_001", action_ts: str = "1700000000"):
    tpl = FIXTURE.read_text(encoding="utf-8")
    return (
        tpl.replace("__COMMAND__", command)
        .replace("__MESSAGE_ID__", message_id)
        .replace("__ACTION_TS__", action_ts)
    )


def make_text_wrapper(text: str):
    return "\n".join(
        [
            f"message in group oc_test_group: {text}",
            "",
            "Conversation info (untrusted metadata):",
            "```json",
            json.dumps(
                {
                    "conversation_label": "oc_test_group",
                    "sender": "tester",
                    "was_mentioned": True,
                },
                ensure_ascii=True,
            ),
            "```",
            "",
            "Sender (untrusted metadata):",
            "```json",
            json.dumps({"name": "Test User", "sender_type": "user"}, ensure_ascii=True),
            "```",
            "",
        ]
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


if __name__ == "__main__":
    unittest.main()
