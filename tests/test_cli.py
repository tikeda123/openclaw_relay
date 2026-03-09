from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from openclaw_relay.cli import main
from openclaw_relay.config import AppConfig
from openclaw_relay.state import StateStore


def write_config(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            [relay]
            node_id = "relay-a"
            state_dir = "./state"
            log_dir = "./log"
            watch_dir = "./watch"
            archive_dir = "./archive"
            deadletter_dir = "./deadletter"
            poll_interval_ms = 500
            health_host = "127.0.0.1"
            health_port = 18080

            [endpoints.a]
            display_name = "OptionABC001"
            base_url = "http://127.0.0.1:31879"
            agent_id = "main"
            default_session_key = "main"
            token_env = "A_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [workers.b]
            display_name = "OptionDEF002"
            base_url = "http://127.0.0.1:31901"
            agent_id = "main"
            default_session_key = "main"
            token_env = "B_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [workers.c]
            display_name = "OptionXYZ003"
            base_url = "http://127.0.0.1:31902"
            agent_id = "main"
            default_session_key = "main"
            token_env = "C_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [routing]
            default_worker = "b"

            [retry]
            max_attempts_b = 5
            max_attempts_a = 10
            initial_backoff_ms = 1000
            max_backoff_ms = 30000
            jitter = true

            [security]
            require_private_ingress = true
            allow_http_only_for_localhost = true
            mask_secrets_in_logs = true

            [audit]
            mode = "preview"
            jsonl_path = "./log/a2a-audit.jsonl"

            [behavior]
            schema_version = "relay-envelope/v1"
            default_ttl_seconds = 300
            duplicate_policy = "suppress"
            inject_notice_on_error = true
            """
        ).strip(),
        encoding="utf-8",
    )


def seed_message(
    store: StateStore,
    *,
    idempotency_key: str,
    status: str,
    worker_name: str,
    worker_display_name: str,
) -> int:
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO messages (
                idempotency_key,
                task_id,
                turn_id,
                filename,
                watch_path,
                status,
                worker_name,
                worker_display_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                idempotency_key,
                f"task-{idempotency_key}",
                f"turn-{idempotency_key}",
                f"{idempotency_key}.json",
                f"/tmp/{idempotency_key}.json",
                status,
                worker_name,
                worker_display_name,
            ),
        )
        connection.commit()
        return int(
            connection.execute(
                "SELECT id FROM messages WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()[0]
        )


class CliTests(unittest.TestCase):
    def test_deadletters_lists_only_requested_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            store = StateStore(config.relay.database_path)
            store.initialize()
            seed_message(
                store,
                idempotency_key="idem-b",
                status="DEADLETTER_B",
                worker_name="b",
                worker_display_name="OptionDEF002",
            )
            seed_message(
                store,
                idempotency_key="idem-c",
                status="DEADLETTER_B",
                worker_name="c",
                worker_display_name="OptionXYZ003",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "deadletters",
                        "--worker",
                        "OptionDEF002",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["worker"], "OptionDEF002")
            self.assertEqual(payload[0]["status"], "DEADLETTER_B")

    def test_replay_deadletter_latest_by_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            store = StateStore(config.relay.database_path)
            store.initialize()

            first_id = seed_message(
                store,
                idempotency_key="idem-first",
                status="DEADLETTER_B",
                worker_name="b",
                worker_display_name="OptionDEF002",
            )
            latest_id = seed_message(
                store,
                idempotency_key="idem-latest",
                status="DEADLETTER_B",
                worker_name="b",
                worker_display_name="OptionDEF002",
            )

            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "replay-deadletter",
                    "--worker",
                    "OptionDEF002",
                    "--latest",
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(store.get_message(first_id)["status"], "DEADLETTER_B")
            self.assertEqual(store.get_message(latest_id)["status"], "RESERVED")


if __name__ == "__main__":
    unittest.main()
