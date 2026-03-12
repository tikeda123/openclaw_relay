from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "client" / "relay-mailbox-worker.py"
)


def load_worker_module():
    spec = importlib.util.spec_from_file_location("relay_mailbox_worker", MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MailboxWorkerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = load_worker_module()

    def test_is_self_addressed_message_returns_true_for_same_sender_and_recipient(self) -> None:
        self.assertTrue(
            self.worker.is_self_addressed_message(
                {
                    "from": "OptionGHI003",
                    "to": "OptionGHI003",
                    "messageId": "MSG-001",
                }
            )
        )

    def test_is_self_addressed_message_returns_false_for_cross_node_message(self) -> None:
        self.assertFalse(
            self.worker.is_self_addressed_message(
                {
                    "from": "OptionABC001",
                    "to": "OptionGHI003",
                    "messageId": "MSG-002",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
