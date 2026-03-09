from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from openclaw_relay.cleanup import ArtifactCleaner
from openclaw_relay.config import RelayConfig
from openclaw_relay.state import StateStore


class ArtifactCleanerTests(unittest.TestCase):
    def test_cleanup_removes_old_artifacts_and_keeps_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relay = RelayConfig(
                node_id="relay-a",
                state_dir=root / "state",
                log_dir=root / "log",
                watch_dir=root / "watch",
                archive_dir=root / "archive",
                deadletter_dir=root / "deadletter",
                poll_interval_ms=100,
                health_host="127.0.0.1",
                health_port=18080,
            )
            for path in (
                relay.state_dir,
                relay.log_dir,
                relay.watch_dir,
                relay.archive_dir,
                relay.deadletter_dir,
                relay.processing_dir,
                relay.replies_dir,
            ):
                path.mkdir(parents=True, exist_ok=True)

            store = StateStore(relay.database_path)
            store.initialize()

            old_archive = relay.archive_dir / "old.json"
            old_archive.write_text("{}", encoding="utf-8")
            old_deadletter = relay.deadletter_dir / "old.json"
            old_deadletter.write_text("{}", encoding="utf-8")
            recent_archive = relay.archive_dir / "recent.json"
            recent_archive.write_text("{}", encoding="utf-8")

            old_processing = relay.processing_dir / "done-old.json"
            old_processing.write_text("{}", encoding="utf-8")
            old_reply = relay.replies_dir / "done-old.json"
            old_reply.write_text("{}", encoding="utf-8")
            recent_processing = relay.processing_dir / "done-recent.json"
            recent_processing.write_text("{}", encoding="utf-8")
            recent_reply = relay.replies_dir / "done-recent.json"
            recent_reply.write_text("{}", encoding="utf-8")

            old_time = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
            recent_time = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
            for path in (old_archive, old_deadletter):
                Path(path).touch()
                import os

                os.utime(path, (old_time, old_time))
            import os

            os.utime(recent_archive, (recent_time, recent_time))

            with store.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO messages (
                        idempotency_key,
                        task_id,
                        turn_id,
                        filename,
                        watch_path,
                        processing_path,
                        reply_path,
                        status,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "done-old",
                        "task-old",
                        "turn-old",
                        "done-old.json",
                        "/tmp/done-old.json",
                        str(old_processing),
                        str(old_reply),
                        "DONE",
                        "2026-02-20 00:00:00",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO messages (
                        idempotency_key,
                        task_id,
                        turn_id,
                        filename,
                        watch_path,
                        processing_path,
                        reply_path,
                        status,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "done-recent",
                        "task-recent",
                        "turn-recent",
                        "done-recent.json",
                        "/tmp/done-recent.json",
                        str(recent_processing),
                        str(recent_reply),
                        "DONE",
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                connection.commit()

            cleaner = ArtifactCleaner(store, relay)
            result = cleaner.cleanup(older_than_days=7)

            self.assertEqual(result.counts["archive"], 1)
            self.assertEqual(result.counts["deadletter"], 1)
            self.assertEqual(result.counts["done_processing"], 1)
            self.assertEqual(result.counts["done_replies"], 1)
            self.assertFalse(old_archive.exists())
            self.assertFalse(old_deadletter.exists())
            self.assertFalse(old_processing.exists())
            self.assertFalse(old_reply.exists())
            self.assertTrue(recent_archive.exists())
            self.assertTrue(recent_processing.exists())
            self.assertTrue(recent_reply.exists())

    def test_cleanup_dry_run_does_not_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relay = RelayConfig(
                node_id="relay-a",
                state_dir=root / "state",
                log_dir=root / "log",
                watch_dir=root / "watch",
                archive_dir=root / "archive",
                deadletter_dir=root / "deadletter",
                poll_interval_ms=100,
                health_host="127.0.0.1",
                health_port=18080,
            )
            relay.archive_dir.mkdir(parents=True, exist_ok=True)
            store = StateStore(relay.database_path)
            store.initialize()

            old_archive = relay.archive_dir / "old.json"
            old_archive.write_text("{}", encoding="utf-8")
            import os

            old_time = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
            os.utime(old_archive, (old_time, old_time))

            cleaner = ArtifactCleaner(store, relay)
            result = cleaner.cleanup(older_than_days=7, dry_run=True)

            self.assertEqual(result.counts["archive"], 1)
            self.assertTrue(old_archive.exists())


if __name__ == "__main__":
    unittest.main()
