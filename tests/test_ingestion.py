from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from openclaw_relay.app import RelayApp
from openclaw_relay.config import (
    AppConfig,
    AuditConfig,
    BehaviorConfig,
    EndpointConfig,
    RelayConfig,
    RetryConfig,
    SSHConnectionConfig,
    SecurityConfig,
    SourceSyncConfig,
    TunnelConfig,
)
from openclaw_relay.response_extractor import ResponseExtractor
from openclaw_relay.responses_client import ResponsesClientError


def build_config(root: Path) -> AppConfig:
    return AppConfig(
        relay=RelayConfig(
            node_id="relay-a",
            state_dir=root / "state",
            log_dir=root / "log",
            watch_dir=root / "watch",
            archive_dir=root / "archive",
            deadletter_dir=root / "deadletter",
            poll_interval_ms=100,
            health_host="127.0.0.1",
            health_port=18080,
        ),
        endpoint_a=EndpointConfig(
            name="a",
            display_name="OptionABC001",
            base_url="http://127.0.0.1:31879",
            agent_id="orchestrator",
            default_session_key="main",
            token_env="A_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        endpoint_b=EndpointConfig(
            name="b",
            display_name="OptionDEF002",
            base_url="http://127.0.0.1:31901",
            agent_id="worker",
            default_session_key="main",
            token_env="B_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        source_sync=None,
        retry=RetryConfig(
            max_attempts_b=5,
            max_attempts_a=10,
            initial_backoff_ms=1000,
            max_backoff_ms=30000,
            jitter=True,
        ),
        security=SecurityConfig(
            require_private_ingress=True,
            allow_http_only_for_localhost=False,
            mask_secrets_in_logs=True,
        ),
        audit=AuditConfig(
            mode="preview",
            jsonl_path=root / "log" / "a2a-audit.jsonl",
        ),
        behavior=BehaviorConfig(
            schema_version="relay-envelope/v1",
            default_ttl_seconds=300,
            duplicate_policy="suppress",
            inject_notice_on_error=True,
        ),
    )


def build_logger() -> logging.Logger:
    logger = logging.getLogger("openclaw_relay_test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


class FakeResponsesClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        self.calls.append((endpoint.display_name, session_key, content))
        return {
            "id": "resp-test",
            "object": "response",
            "created_at": 1772896430,
            "status": "completed",
            "model": f"openclaw:{endpoint.agent_id}",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "pong",
                        }
                    ],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


class FlakyResponsesClient:
    def __init__(self, *, fail_b_times: int = 0, fail_a_times: int = 0) -> None:
        self.fail_b_times = fail_b_times
        self.fail_a_times = fail_a_times
        self.calls: list[tuple[str, str, str]] = []

    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        self.calls.append((endpoint.display_name, session_key, content))
        if endpoint.display_name == "OptionDEF002" and self.fail_b_times > 0:
            self.fail_b_times -= 1
            raise ResponsesClientError("temporary B failure")
        if endpoint.display_name == "OptionABC001" and self.fail_a_times > 0:
            self.fail_a_times -= 1
            raise ResponsesClientError("temporary A failure")
        return {
            "id": "resp-flaky",
            "object": "response",
            "created_at": 1772896430,
            "status": "completed",
            "model": f"openclaw:{endpoint.agent_id}",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "pong",
                        }
                    ],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


class RecordingRemoteOutboxSyncer:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self, config, local_watch_dir: str):
        self.calls += 1
        return []


class RotatingTokenResolver:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = list(tokens)
        self.calls = 0

    def fetch_token(self, tunnel) -> str:
        self.calls += 1
        if not self.tokens:
            raise AssertionError("no token left to return")
        return self.tokens.pop(0)


class TokenAwareResponsesClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        self.calls.append((endpoint.display_name, token))
        if endpoint.display_name == "OptionDEF002" and token == "stale-token":
            raise ResponsesClientError("OptionDEF002 returned HTTP 401: unauthorized", http_status=401)
        return {
            "id": "resp-token-aware",
            "object": "response",
            "created_at": 1772896430,
            "status": "completed",
            "model": f"openclaw:{endpoint.agent_id}",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "pong",
                        }
                    ],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


def write_envelope(path: Path, *, idempotency_key: str, created_at: datetime | None = None) -> None:
    payload = {
        "schemaVersion": "relay-envelope/v1",
        "taskId": "TASK-20260307-001",
        "turnId": "TURN-001",
        "fromGateway": "OptionABC001",
        "fromAgent": "orchestrator",
        "toGateway": "OptionDEF002",
        "toAgent": "worker",
        "intent": "request",
        "body": "compare three design options",
        "stateRef": "TASK-20260307-001#rev1",
        "returnSessionKey": "main",
        "approvalRequired": False,
        "ttlSeconds": 300,
        "idempotencyKey": idempotency_key,
        "createdAt": (created_at or datetime.now(timezone.utc)).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def source_key_for(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stat = path.stat()
    return f"{path.resolve()}:{stat.st_size}:{digest}"


class IngestionTests(unittest.TestCase):
    def test_valid_envelope_is_reserved_and_copied_to_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-001")

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            self.assertTrue(source.exists())
            processing_copy = config.relay.processing_dir / "message.json"
            self.assertTrue(processing_copy.exists())

            messages = app.store.list_messages()
            seen_files = app.store.list_seen_files()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["status"], "RESERVED")
            self.assertEqual(messages[0]["processing_path"], str(processing_copy))
            self.assertEqual(len(seen_files), 1)
            self.assertEqual(seen_files[0]["status"], "RESERVED")

            processed_again = app.poll_once()
            self.assertEqual(processed_again, 0)

    def test_duplicate_idempotency_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            first = config.relay.watch_dir / "message-1.json"
            second = config.relay.watch_dir / "message-2.json"
            write_envelope(first, idempotency_key="idem-dup")
            write_envelope(second, idempotency_key="idem-dup")

            processed = app.poll_once()

            self.assertEqual(processed, 2)
            archived = sorted(config.relay.archive_dir.glob("message-2__duplicate_suppressed*.json"))
            self.assertEqual(len(archived), 1)
            self.assertTrue((config.relay.processing_dir / "message-1.json").exists())
            self.assertEqual(len(app.store.list_messages()), 1)
            seen_files = app.store.list_seen_files()
            self.assertEqual(len(seen_files), 2)
            self.assertEqual(seen_files[1]["status"], "DUPLICATE_SUPPRESSED")

    def test_invalid_or_expired_envelope_goes_to_deadletter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            invalid = config.relay.watch_dir / "invalid.json"
            expired = config.relay.watch_dir / "expired.json"
            invalid.write_text("{", encoding="utf-8")
            write_envelope(
                expired,
                idempotency_key="idem-expired",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )

            processed = app.poll_once()

            self.assertEqual(processed, 2)
            deadletters = {path.name for path in config.relay.deadletter_dir.glob("*.json")}
            self.assertIn("invalid__invalid_json.json", deadletters)
            self.assertIn("expired__expired.json", deadletters)
            self.assertEqual(len(app.store.list_messages()), 0)
            statuses = sorted(row["status"] for row in app.store.list_seen_files())
            self.assertEqual(statuses, ["EXPIRED", "INVALID_JSON"])

    def test_resume_after_partial_crash_finalizes_existing_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-resume")

            stat = source.stat()
            source_key = source_key_for(source)
            app.store.claim_source_file(
                source_key=source_key,
                filename=source.name,
                watch_path=str(source.resolve()),
                source_size=stat.st_size,
                source_mtime_ns=stat.st_mtime_ns,
            )
            from openclaw_relay.envelope import Envelope

            envelope = Envelope.from_file(source, expected_schema_version=config.behavior.schema_version)
            reservation = app.store.reserve_message(
                envelope,
                filename=source.name,
                watch_path=str(source.resolve()),
            )
            self.assertTrue(reservation.message_id > 0)

            processing_copy = config.relay.processing_dir / source.name
            processing_copy.parent.mkdir(parents=True, exist_ok=True)
            processing_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            seen_files = app.store.list_seen_files()
            messages = app.store.list_messages()
            self.assertEqual(seen_files[0]["status"], "RESERVED")
            self.assertEqual(seen_files[0]["local_copy_path"], str(processing_copy))
            self.assertEqual(messages[0]["processing_path"], str(processing_copy))
            self.assertEqual(messages[0]["status"], "RESERVED")

    def test_same_content_with_new_mtime_is_not_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-stable")

            first_processed = app.poll_once()
            self.assertEqual(first_processed, 1)

            original_text = source.read_text(encoding="utf-8")
            source.write_text(original_text, encoding="utf-8")

            second_processed = app.poll_once()
            self.assertEqual(second_processed, 0)
            self.assertEqual(len(app.store.list_seen_files()), 1)

    def test_reserved_message_is_dispatched_and_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            fake_client = FakeResponsesClient()
            app = RelayApp(
                config,
                build_logger(),
                responses_client=fake_client,
                response_extractor=ResponseExtractor(),
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-e2e")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ):
                processed = app.poll_once()

            self.assertEqual(processed, 3)
            messages = app.store.list_messages()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["status"], "DONE")
            self.assertEqual(messages[0]["reply_text"], "pong")
            self.assertTrue(Path(messages[0]["reply_path"]).exists())
            self.assertEqual(len(fake_client.calls), 2)
            self.assertEqual(fake_client.calls[0][0], "OptionDEF002")
            self.assertEqual(fake_client.calls[1][0], "OptionABC001")
            self.assertEqual(fake_client.calls[1][1], "main")

    def test_dispatch_retry_then_success_records_attempts_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config = replace(
                config,
                retry=replace(
                    config.retry,
                    initial_backoff_ms=1,
                    max_backoff_ms=1,
                    jitter=False,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            flaky_client = FlakyResponsesClient(fail_b_times=1)
            app = RelayApp(
                config,
                build_logger(),
                responses_client=flaky_client,
                response_extractor=ResponseExtractor(),
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-retry")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ):
                first_processed = app.poll_once()
                time.sleep(0.01)
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 1)
            self.assertEqual(second_processed, 2)
            message = app.store.get_message_by_idempotency_key("idem-retry")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")
            attempts = app.store.list_attempts(message_id=message["id"])
            self.assertEqual(
                [(row["target"], row["attempt_no"], row["result"]) for row in attempts],
                [
                    ("B", 1, "FAILED"),
                    ("B", 2, "SUCCESS"),
                    ("A", 1, "SUCCESS"),
                ],
            )
            audit_lines = [
                json.loads(line)
                for line in config.audit.jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = {line["eventType"] for line in audit_lines}
            self.assertIn("dispatch_retry_scheduled", event_types)
            self.assertIn("dispatch_succeeded", event_types)
            self.assertIn("inject_succeeded", event_types)

    def test_terminal_dispatch_failure_goes_to_deadletter_and_can_be_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config = replace(
                config,
                retry=replace(
                    config.retry,
                    max_attempts_b=2,
                    initial_backoff_ms=1,
                    max_backoff_ms=1,
                    jitter=False,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            flaky_client = FlakyResponsesClient(fail_b_times=99)
            app = RelayApp(
                config,
                build_logger(),
                responses_client=flaky_client,
                response_extractor=ResponseExtractor(),
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-deadletter")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ):
                first_processed = app.poll_once()
                time.sleep(0.01)
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 1)
            self.assertEqual(second_processed, 0)
            message = app.store.get_message_by_idempotency_key("idem-deadletter")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DEADLETTER_B")
            self.assertEqual(app.store.count_attempts(message_id=message["id"], target="B"), 2)
            self.assertTrue(any(path.name.startswith("message__failed_b") for path in config.relay.deadletter_dir.glob("*.json")))

            next_status = app.replay_deadletter(message["id"])
            self.assertEqual(next_status, "RESERVED")
            replayed = app.store.get_message(message["id"])
            self.assertEqual(replayed["status"], "RESERVED")
            self.assertEqual(app.store.count_attempts(message_id=message["id"], target="B"), 0)

    def test_metrics_pending_watch_counts_only_unhandled_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-metrics-pending")

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            metrics = app.metrics_text()
            self.assertIn("openclaw_relay_watch_present_files 1", metrics)
            self.assertIn("openclaw_relay_watch_pending_files 0", metrics)

    def test_source_sync_is_throttled_between_polls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                build_config(root),
                source_sync=SourceSyncConfig(
                    ssh=SSHConnectionConfig(
                        host="optionabc001",
                        user="controluser",
                        key_path=root / "dummy-key",
                        connect_timeout_seconds=10,
                        strict_host_key_checking=True,
                    ),
                    remote_path="/remote/outbox/pending",
                    sync_interval_ms=60_000,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            syncer = RecordingRemoteOutboxSyncer()
            app = RelayApp(
                config,
                build_logger(),
                remote_outbox_syncer=syncer,
            )
            app.initialize()

            with mock.patch.object(app, "_refresh_external_health"):
                app.poll_once()
                app.poll_once()

            self.assertEqual(syncer.calls, 1)

    def test_dispatch_refreshes_remote_token_after_401(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config = replace(
                config,
                endpoint_b=replace(
                    config.endpoint_b,
                    tunnel=TunnelConfig(
                    ssh=SSHConnectionConfig(
                        host="optiondef002",
                        user="workeruser",
                        key_path=root / "id_ed25519_optiondef002",
                        connect_timeout_seconds=10,
                        strict_host_key_checking=True,
                        ),
                        local_port=31901,
                        remote_host="127.0.0.1",
                        remote_port=18789,
                        token_config_path=Path("/home/workeruser/.openclaw/openclaw.json"),
                    ),
                ),
                retry=replace(
                    config.retry,
                    initial_backoff_ms=1,
                    max_backoff_ms=1,
                    jitter=False,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            resolver = RotatingTokenResolver(["stale-token", "fresh-token"])
            client = TokenAwareResponsesClient()
            app = RelayApp(
                config,
                build_logger(),
                responses_client=client,
                response_extractor=ResponseExtractor(),
                remote_token_resolver=resolver,
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_envelope(source, idempotency_key="idem-token-refresh")

            with mock.patch.dict(os.environ, {"A_GATEWAY_TOKEN": "dummy-a"}, clear=False):
                first_processed = app.poll_once()
                time.sleep(0.01)
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 1)
            self.assertEqual(second_processed, 2)
            self.assertEqual(resolver.calls, 2)
            self.assertEqual(
                client.calls,
                [
                    ("OptionDEF002", "stale-token"),
                    ("OptionDEF002", "fresh-token"),
                    ("OptionABC001", "dummy-a"),
                ],
            )
            message = app.store.get_message_by_idempotency_key("idem-token-refresh")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")


if __name__ == "__main__":
    unittest.main()
