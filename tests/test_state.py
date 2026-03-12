from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from openclaw_relay.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_initialize_creates_expected_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "relay.db"
            store = StateStore(database_path)

            store.initialize()

            tables = store.list_tables()
            self.assertIn("messages", tables)
            self.assertIn("seen_files", tables)
            self.assertIn("attempts", tables)
            self.assertIn("audit_index", tables)
            self.assertIn("mailbox_messages", tables)

    def test_attempt_and_audit_records_can_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "relay.db"
            store = StateStore(database_path)
            store.initialize()

            with store.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO messages (
                        idempotency_key,
                        task_id,
                        turn_id,
                        filename,
                        watch_path,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("idem-1", "task-1", "turn-1", "message.json", "/tmp/message.json", "RESERVED"),
                )
                connection.commit()
                message_id = int(
                    connection.execute("SELECT id FROM messages WHERE idempotency_key = 'idem-1'").fetchone()[0]
                )

            attempt_no = store.record_attempt(
                message_id=message_id,
                target="B",
                result="FAILED",
                error_text="boom",
            )
            store.record_audit_event(
                message_id=message_id,
                event_type="dispatch_retry_scheduled",
                event_ref="evt-1",
            )

            self.assertEqual(attempt_no, 1)
            self.assertEqual(store.count_attempts(message_id=message_id, target="B"), 1)
            self.assertEqual(store.list_attempts(message_id=message_id)[0]["error_text"], "boom")
            self.assertEqual(store.list_audit_events(message_id=message_id)[0]["event_ref"], "evt-1")

    def test_update_message_status_persists_next_attempt_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "relay.db"
            store = StateStore(database_path)
            store.initialize()

            with store.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO messages (
                        idempotency_key,
                        task_id,
                        turn_id,
                        filename,
                        watch_path,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("idem-2", "task-2", "turn-2", "message.json", "/tmp/message.json", "RESERVED"),
                )
                connection.commit()
                message_id = int(
                    connection.execute("SELECT id FROM messages WHERE idempotency_key = 'idem-2'").fetchone()[0]
                )

            store.update_message_status(
                message_id=message_id,
                status="FAILED_B",
                error_text="temporary",
                next_attempt_at="2026-03-09T00:00:00Z",
            )
            row = store.get_message(message_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["status"], "FAILED_B")
            self.assertEqual(row["next_attempt_at"], "2026-03-09T00:00:00Z")

    def test_mailbox_messages_are_dequeued_fifo_and_removed_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "relay.db"
            store = StateStore(database_path)
            store.initialize()

            store.enqueue_mailbox_message(
                message_id="msg-1",
                conversation_id="conv-1",
                from_mailbox="OptionABC001",
                to_mailbox="OptionDEF002",
                body="first",
                queued_at="2026-03-11T13:00:00+00:00",
            )
            store.enqueue_mailbox_message(
                message_id="msg-2",
                conversation_id="conv-2",
                from_mailbox="OptionABC001",
                to_mailbox="OptionDEF002",
                body="second",
                queued_at="2026-03-11T13:00:01+00:00",
            )

            first = store.dequeue_mailbox_message(mailbox="OptionDEF002")
            second = store.dequeue_mailbox_message(mailbox="OptionDEF002")
            third = store.dequeue_mailbox_message(mailbox="OptionDEF002")

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None
            assert second is not None
            self.assertEqual(first["message_id"], "msg-1")
            self.assertEqual(second["message_id"], "msg-2")
            self.assertEqual(first["status"], "delivered")
            self.assertEqual(second["status"], "delivered")
            self.assertIsNotNone(first["dequeued_at"])
            self.assertIsNotNone(second["dequeued_at"])
            self.assertIsNone(third)
            self.assertEqual(store.count_mailbox_queue_depths(), {})
            self.assertEqual(store.count_mailbox_messages_by_status()["delivered"], 2)


if __name__ == "__main__":
    unittest.main()
