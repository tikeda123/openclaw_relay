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


if __name__ == "__main__":
    unittest.main()
