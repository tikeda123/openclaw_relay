from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openclaw_relay.envelope import Envelope


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT UNIQUE NOT NULL,
        task_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        watch_path TEXT NOT NULL,
        processing_path TEXT,
        status TEXT NOT NULL,
        last_error TEXT,
        next_attempt_at TEXT,
        worker_name TEXT,
        worker_display_name TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_key TEXT UNIQUE NOT NULL,
        filename TEXT NOT NULL,
        watch_path TEXT NOT NULL,
        source_size INTEGER NOT NULL,
        source_mtime_ns INTEGER NOT NULL,
        local_copy_path TEXT,
        status TEXT NOT NULL,
        message_id INTEGER,
        error_text TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(message_id) REFERENCES messages(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL,
        target TEXT NOT NULL,
        attempt_no INTEGER NOT NULL,
        result TEXT NOT NULL,
        error_text TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(message_id) REFERENCES messages(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_index (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER,
        event_type TEXT NOT NULL,
        event_ref TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(message_id) REFERENCES messages(id)
    )
    """,
)


@dataclass(frozen=True)
class ReserveMessageResult:
    reserved: bool
    message_id: int


@dataclass
class StateStore:
    database_path: Path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            self._migrate(connection)
            connection.commit()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def healthcheck(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def list_tables(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def _migrate(self, connection: sqlite3.Connection) -> None:
        self._ensure_columns(
            connection,
            table_name="messages",
            columns=(
                ("processing_path", "TEXT"),
                ("reply_path", "TEXT"),
                ("reply_text", "TEXT"),
                ("last_error", "TEXT"),
                ("next_attempt_at", "TEXT"),
                ("worker_name", "TEXT"),
                ("worker_display_name", "TEXT"),
            ),
        )
        self._ensure_columns(
            connection,
            table_name="seen_files",
            columns=(
                ("local_copy_path", "TEXT"),
                ("message_id", "INTEGER"),
                ("error_text", "TEXT"),
            ),
        )

    def _ensure_columns(
        self,
        connection: sqlite3.Connection,
        *,
        table_name: str,
        columns: tuple[tuple[str, str], ...],
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, column_type in columns:
            if column_name not in existing:
                connection.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )

    def claim_source_file(
        self,
        *,
        source_key: str,
        filename: str,
        watch_path: str,
        source_size: int,
        source_mtime_ns: int,
    ) -> bool:
        with self.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO seen_files (
                        source_key,
                        filename,
                        watch_path,
                        source_size,
                        source_mtime_ns,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        filename,
                        watch_path,
                        source_size,
                        source_mtime_ns,
                        "DISCOVERED",
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            connection.commit()
        return True

    def finalize_source_file(
        self,
        *,
        source_key: str,
        status: str,
        message_id: int | None = None,
        error_text: str | None = None,
        local_copy_path: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE seen_files
                SET
                    status = ?,
                    message_id = ?,
                    error_text = ?,
                    local_copy_path = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_key = ?
                """,
                (status, message_id, error_text, local_copy_path, source_key),
            )
            connection.commit()

    def get_seen_file(self, source_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    source_key,
                    filename,
                    watch_path,
                    local_copy_path,
                    status,
                    message_id,
                    error_text
                FROM seen_files
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()

    def reserve_message(self, envelope: "Envelope", *, filename: str, watch_path: str) -> ReserveMessageResult:
        with self.connect() as connection:
            try:
                cursor = connection.execute(
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
                    (
                        envelope.idempotency_key,
                        envelope.task_id,
                        envelope.turn_id,
                        filename,
                        watch_path,
                        "RESERVED",
                    ),
                )
            except sqlite3.IntegrityError:
                existing_row = connection.execute(
                    "SELECT id FROM messages WHERE idempotency_key = ?",
                    (envelope.idempotency_key,),
                ).fetchone()
                if existing_row is None:
                    raise
                return ReserveMessageResult(reserved=False, message_id=existing_row["id"])

            connection.commit()
            return ReserveMessageResult(reserved=True, message_id=int(cursor.lastrowid))

    def update_message_artifact(
        self,
        *,
        message_id: int,
        processing_path: str,
        status: str = "RESERVED",
        error_text: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET
                    processing_path = ?,
                    status = ?,
                    last_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (processing_path, status, error_text, message_id),
            )
            connection.commit()

    def update_message_reply(
        self,
        *,
        message_id: int,
        reply_path: str,
        reply_text: str,
        status: str = "B_REPLIED",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET
                    reply_path = ?,
                    reply_text = ?,
                    status = ?,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reply_path, reply_text, status, message_id),
            )
            connection.commit()

    def assign_message_worker(
        self,
        *,
        message_id: int,
        worker_name: str,
        worker_display_name: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET
                    worker_name = ?,
                    worker_display_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (worker_name, worker_display_name, message_id),
            )
            connection.commit()

    def update_message_status(
        self,
        *,
        message_id: int,
        status: str,
        error_text: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET
                    status = ?,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error_text, next_attempt_at, message_id),
            )
            connection.commit()

    def list_messages(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    idempotency_key,
                    task_id,
                    turn_id,
                    filename,
                    watch_path,
                    processing_path,
                    reply_path,
                    reply_text,
                    status,
                    last_error,
                    next_attempt_at,
                    worker_name,
                    worker_display_name,
                    created_at,
                    updated_at
                FROM messages
                ORDER BY id
                """
            ).fetchall()

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    idempotency_key,
                    task_id,
                    turn_id,
                    filename,
                    watch_path,
                    processing_path,
                    reply_path,
                    reply_text,
                    status,
                    last_error,
                    next_attempt_at,
                    worker_name,
                    worker_display_name,
                    created_at,
                    updated_at
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

    def list_messages_by_status(self, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            return connection.execute(
                f"""
                SELECT
                    id,
                    idempotency_key,
                    task_id,
                    turn_id,
                    filename,
                    watch_path,
                    processing_path,
                    reply_path,
                    reply_text,
                    status,
                    last_error,
                    next_attempt_at,
                    worker_name,
                    worker_display_name,
                    created_at,
                    updated_at
                FROM messages
                WHERE status IN ({placeholders})
                ORDER BY id
                """,
                statuses,
            ).fetchall()

    def count_messages_by_status(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM messages
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def get_message_by_idempotency_key(self, idempotency_key: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    idempotency_key,
                    task_id,
                    turn_id,
                    filename,
                    watch_path,
                    processing_path,
                    reply_path,
                    reply_text,
                    status,
                    last_error,
                    next_attempt_at,
                    worker_name,
                    worker_display_name,
                    created_at,
                    updated_at
                FROM messages
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()

    def list_seen_files(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    source_key,
                    filename,
                    watch_path,
                    local_copy_path,
                    status,
                    message_id,
                    error_text
                FROM seen_files
                ORDER BY id
                """
            ).fetchall()

    def count_seen_files_by_status(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM seen_files
                GROUP BY status
                ORDER BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def record_attempt(
        self,
        *,
        message_id: int,
        target: str,
        result: str,
        error_text: str | None = None,
    ) -> int:
        with self.connect() as connection:
            next_attempt = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1
                FROM attempts
                WHERE message_id = ? AND target = ?
                """,
                (message_id, target),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO attempts (
                    message_id,
                    target,
                    attempt_no,
                    result,
                    error_text,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    target,
                    next_attempt,
                    result,
                    error_text,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        return int(next_attempt)

    def count_attempts(self, *, message_id: int, target: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM attempts
                WHERE message_id = ? AND target = ?
                """,
                (message_id, target),
            ).fetchone()
        return int(row["count"])

    def list_attempts(self, *, message_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    message_id,
                    target,
                    attempt_no,
                    result,
                    error_text,
                    created_at
                FROM attempts
                WHERE message_id = ?
                ORDER BY id
                """,
                (message_id,),
            ).fetchall()

    def list_all_attempts(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    message_id,
                    target,
                    attempt_no,
                    result,
                    error_text,
                    created_at
                FROM attempts
                ORDER BY id
                """
            ).fetchall()

    def count_attempts_by_target_result(self) -> dict[tuple[str, str], int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT target, result, COUNT(*) AS count
                FROM attempts
                GROUP BY target, result
                ORDER BY target, result
                """
            ).fetchall()
        return {
            (str(row["target"]), str(row["result"])): int(row["count"])
            for row in rows
        }

    def count_attempts_by_message_target_result(self) -> dict[tuple[int, str, str], int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT message_id, target, result, COUNT(*) AS count
                FROM attempts
                GROUP BY message_id, target, result
                ORDER BY message_id, target, result
                """
            ).fetchall()
        return {
            (int(row["message_id"]), str(row["target"]), str(row["result"])): int(row["count"])
            for row in rows
        }

    def clear_attempts(self, *, message_id: int, target: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM attempts WHERE message_id = ? AND target = ?",
                (message_id, target),
            )
            connection.commit()

    def record_audit_event(
        self,
        *,
        message_id: int | None,
        event_type: str,
        event_ref: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_index (
                    message_id,
                    event_type,
                    event_ref
                ) VALUES (?, ?, ?)
                """,
                (message_id, event_type, event_ref),
            )
            connection.commit()

    def list_audit_events(self, *, message_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        sql = """
            SELECT
                id,
                message_id,
                event_type,
                event_ref,
                created_at
            FROM audit_index
        """
        params: tuple[object, ...]
        if message_id is None:
            sql += " ORDER BY id DESC LIMIT ?"
            params = (limit,)
        else:
            sql += " WHERE message_id = ? ORDER BY id DESC LIMIT ?"
            params = (message_id, limit)
        with self.connect() as connection:
            return connection.execute(sql, params).fetchall()
