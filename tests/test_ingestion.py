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
    RabbitMQConfig,
    RelayConfig,
    RetryConfig,
    SSHConnectionConfig,
    SecurityConfig,
    TunnelConfig,
)
from openclaw_relay.envelope import (
    DEFAULT_HUMAN_SESSION_KEY,
    MESSAGE_SCHEMA_VERSION,
    Envelope,
    build_relay_session_key,
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
            default_session_key="agent:main:main",
            token_env="A_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        endpoint_b=EndpointConfig(
            name="b",
            display_name="OptionDEF002",
            base_url="http://127.0.0.1:31901",
            agent_id="worker",
            default_session_key="agent:main:main",
            token_env="B_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
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
            schema_version=MESSAGE_SCHEMA_VERSION,
            default_ttl_seconds=300,
            duplicate_policy="suppress",
            inject_notice_on_error=True,
        ),
    )


def build_rabbitmq_config(root: Path) -> AppConfig:
    return replace(
        build_config(root),
        transport_mode="rabbitmq",
        rabbitmq=RabbitMQConfig(
            host="127.0.0.1",
            port=5672,
            virtual_host="/openclaw-relay",
            user_env="RABBITMQ_USER",
            password_env="RABBITMQ_PASSWORD",
            heartbeat_seconds=30,
            blocked_connection_timeout_seconds=30,
            prefetch_count=4,
            queue_type="quorum",
            dispatch_exchange="relay.dispatch.direct",
            reply_exchange="relay.reply.direct",
            mailbox_exchange="relay.mailbox.direct",
            deadletter_exchange="relay.dead.direct",
            events_exchange="relay.events.topic",
            mailbox_queue_prefix="relay.mailbox",
            worker_queue_prefix="relay.worker",
            control_queue_prefix="relay.control",
            deadletter_queue_prefix="relay.deadletter",
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


class InjectTimeoutResponsesClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        self.calls.append((endpoint.display_name, session_key, content))
        if endpoint.display_name == "OptionABC001":
            raise ResponsesClientError("timed out")
        return {
            "id": "resp-inject-timeout",
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
                            "text": "ACK",
                        }
                    ],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


class SequenceSessionFetcher:
    def __init__(self, found_sequence: list[bool]) -> None:
        self.found_sequence = list(found_sequence)
        self.lookups: list[tuple[str, str]] = []

    def find_message(self, connection, *, session_key: str, marker_text: str, limit_files: int = 32):
        self.lookups.append((session_key, marker_text))
        found = self.found_sequence.pop(0) if self.found_sequence else False
        if not found:
            return None
        return {
            "found": True,
            "sessionFile": "relay-optiondef002.jsonl",
            "at": "2026-03-09T10:01:17Z",
            "role": "user",
            "body": marker_text,
        }


class DispatchTimeoutResponsesClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        self.calls.append((endpoint.display_name, session_key, content))
        if endpoint.display_name == "OptionDEF002":
            raise ResponsesClientError("timed out")
        return {
            "id": "resp-dispatch-timeout",
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
                            "text": "ACK",
                        }
                    ],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


class WorkerReplySessionFetcher:
    def __init__(self, reply_sequence: list[dict[str, object] | None]) -> None:
        self.reply_sequence = list(reply_sequence)
        self.reply_lookups: list[str] = []

    def find_message_reply(self, connection, *, marker_text: str, limit_files: int = 16):
        self.reply_lookups.append(marker_text)
        return self.reply_sequence.pop(0) if self.reply_sequence else None


class FakeBrokerDelivery:
    def __init__(self, payload: dict[str, object]):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.acked = False
        self.nacked = None

    def ack(self) -> None:
        self.acked = True

    def nack(self, *, requeue: bool) -> None:
        self.nacked = requeue


class FakeRabbitMQBroker:
    def __init__(self) -> None:
        self.published_messages: list[dict[str, object]] = []
        self.reply_queue: list[FakeBrokerDelivery] = []

    def relay_reply_binding(self):
        return type("Binding", (), {"queue": "relay.control.optionabc001.reply"})

    def publish_message(self, payload: dict[str, object]) -> dict[str, object]:
        self.published_messages.append(payload)
        return {
            "exchange": "relay.dispatch.direct",
            "routingKey": "optiondef002",
            "replyTo": "relay.control.optionabc001.reply",
            "messageId": str(payload["messageId"]),
            "conversationId": str(payload["conversationId"]),
            "toGateway": str(payload["toGateway"]),
        }

    def consume_one(self, queue_name: str):
        if queue_name != "relay.control.optionabc001.reply":
            raise AssertionError(f"unexpected queue: {queue_name}")
        if not self.reply_queue:
            return None
        return self.reply_queue.pop(0)


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


def write_message(path: Path, *, message_id: str, created_at: datetime | None = None) -> None:
    payload = {
        "schemaVersion": MESSAGE_SCHEMA_VERSION,
        "conversationId": "CONV-20260307-001",
        "messageId": message_id,
        "fromGateway": "OptionABC001",
        "toGateway": "OptionDEF002",
        "body": "compare three design options",
        "idempotencyKey": message_id,
        "ttlSeconds": 300,
        "createdAt": (created_at or datetime.now(timezone.utc)).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_minimal_message(path: Path, *, to_gateway: str = "OptionDEF002", body: str = "minimal hello") -> None:
    payload = {
        "toGateway": to_gateway,
        "body": body,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_public_message(
    path: Path,
    *,
    message_id: str,
    from_name: str = "OptionABC001",
    to_name: str = "OptionDEF002",
    body: str = "public fields message",
    created_at: datetime | None = None,
) -> None:
    payload = {
        "schemaVersion": MESSAGE_SCHEMA_VERSION,
        "conversationId": f"CONV-{message_id}",
        "messageId": message_id,
        "from": from_name,
        "to": to_name,
        "body": body,
        "idempotencyKey": message_id,
        "ttlSeconds": 300,
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

    def test_message_schema_is_reserved_and_copied_to_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "message-v2.json"
            write_message(source, message_id="msg-001")

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            messages = app.store.list_messages()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["task_id"], "CONV-20260307-001")
            self.assertEqual(messages[0]["turn_id"], "msg-001")
            self.assertEqual(messages[0]["idempotency_key"], "msg-001")
            envelope = Envelope.from_file(
                config.relay.processing_dir / "message-v2.json",
                expected_schema_version=config.behavior.schema_version,
            )
            self.assertEqual(
                envelope.return_session_key,
                DEFAULT_HUMAN_SESSION_KEY,
            )

    def test_minimal_message_input_is_accepted_and_metadata_is_auto_filled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "simple-hello.json"
            write_minimal_message(source, body="hello from minimal")

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            messages = app.store.list_messages()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["status"], "RESERVED")
            envelope = Envelope.from_file(
                config.relay.processing_dir / "simple-hello.json",
                expected_schema_version=config.behavior.schema_version,
            )
            self.assertEqual(envelope.schema_version, MESSAGE_SCHEMA_VERSION)
            self.assertEqual(envelope.from_gateway, "OptionABC001")
            self.assertEqual(envelope.to_gateway, "OptionDEF002")
            self.assertEqual(envelope.body, "hello from minimal")
            self.assertTrue(envelope.message_id.startswith("MSG-simple-hello-"))
            self.assertEqual(envelope.conversation_id, envelope.message_id)
            self.assertEqual(envelope.idempotency_key, envelope.message_id)

    def test_public_from_to_message_input_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            app = RelayApp(config, build_logger())
            app.initialize()

            source = config.relay.watch_dir / "public-fields.json"
            write_public_message(
                source,
                message_id="MSG-PUBLIC-001",
                body="hello from public fields",
            )

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            envelope = Envelope.from_file(
                config.relay.processing_dir / "public-fields.json",
                expected_schema_version=config.behavior.schema_version,
            )
            self.assertEqual(envelope.from_gateway, "OptionABC001")
            self.assertEqual(envelope.to_gateway, "OptionDEF002")
            self.assertEqual(envelope.body, "hello from public fields")
            processing_payload = json.loads(
                (config.relay.processing_dir / "public-fields.json").read_text(encoding="utf-8")
            )
            self.assertEqual(processing_payload["from"], "OptionABC001")
            self.assertEqual(processing_payload["to"], "OptionDEF002")
            self.assertEqual(processing_payload["fromGateway"], "OptionABC001")
            self.assertEqual(processing_payload["toGateway"], "OptionDEF002")

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
            self.assertEqual(
                fake_client.calls[1][1],
                DEFAULT_HUMAN_SESSION_KEY,
            )

    def test_rabbitmq_transport_queues_message_without_sync_worker_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_rabbitmq_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            fake_client = FakeResponsesClient()
            fake_broker = FakeRabbitMQBroker()
            app = RelayApp(
                config,
                build_logger(),
                responses_client=fake_client,
                response_extractor=ResponseExtractor(),
            )
            app._rabbitmq_broker = fake_broker
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="msg-rmq-001")

            processed = app.poll_once()

            self.assertEqual(processed, 2)
            messages = app.store.list_messages()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["status"], "QUEUED_B")
            self.assertEqual(fake_client.calls, [])
            self.assertEqual(len(fake_broker.published_messages), 1)
            self.assertEqual(fake_broker.published_messages[0]["messageId"], "msg-rmq-001")

    def test_rabbitmq_reply_transitions_to_done_via_existing_inject_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_rabbitmq_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            fake_client = FakeResponsesClient()
            fake_broker = FakeRabbitMQBroker()
            app = RelayApp(
                config,
                build_logger(),
                responses_client=fake_client,
                response_extractor=ResponseExtractor(),
            )
            app._rabbitmq_broker = fake_broker
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="msg-rmq-002")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                },
                clear=False,
            ):
                app.poll_once()
                fake_broker.reply_queue.append(
                    FakeBrokerDelivery(
                        {
                            "schemaVersion": "relay-reply/v1",
                            "status": "ok",
                            "conversationId": "CONV-20260307-001",
                            "messageId": "msg-rmq-002",
                            "fromGateway": "OptionDEF002",
                            "toGateway": "OptionABC001",
                            "returnSessionKey": build_relay_session_key(
                                to_gateway="OptionDEF002",
                                conversation_id="CONV-20260307-001",
                            ),
                            "createdAt": datetime.now(timezone.utc).isoformat(),
                            "replyText": "pong from queue",
                        }
                    )
                )
                processed = app.poll_once()

            self.assertEqual(processed, 2)
            message = app.store.get_message_by_turn_id("msg-rmq-002")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")
            self.assertEqual(message["reply_text"], "pong from queue")
            self.assertEqual(len(fake_client.calls), 1)
            self.assertEqual(fake_client.calls[0][0], "OptionABC001")
            self.assertIn("pong from queue", fake_client.calls[0][2])

    def test_rabbitmq_queued_message_is_completed_from_worker_session_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_config = build_rabbitmq_config(root)
            config = replace(
                base_config,
                endpoint_b=replace(
                    base_config.endpoint_b,
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
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            fake_client = FakeResponsesClient()
            fake_broker = FakeRabbitMQBroker()
            session_fetcher = WorkerReplySessionFetcher(
                [
                    None,
                    {
                        "found": True,
                        "sessionFile": "worker-main.jsonl",
                        "requestAt": "2026-03-10T00:42:18Z",
                        "replyAt": "2026-03-10T00:43:05Z",
                        "replyText": "analysis-ok",
                    },
                ]
            )
            app = RelayApp(
                config,
                build_logger(),
                responses_client=fake_client,
                response_extractor=ResponseExtractor(),
                remote_session_fetcher=session_fetcher,
            )
            app._rabbitmq_broker = fake_broker
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="msg-rmq-004")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                },
                clear=False,
            ):
                first_processed = app.poll_once()
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 2)
            self.assertEqual(second_processed, 2)
            message = app.store.get_message_by_turn_id("msg-rmq-004")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")
            self.assertEqual(message["reply_text"], "analysis-ok")
            self.assertEqual(len(fake_broker.published_messages), 1)
            self.assertEqual(len(fake_client.calls), 1)
            self.assertEqual(fake_client.calls[0][0], "OptionABC001")
            self.assertIn("analysis-ok", fake_client.calls[0][2])
            self.assertEqual(len(session_fetcher.reply_lookups), 2)

    def test_rabbitmq_error_reply_schedules_retry_without_republishing_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_rabbitmq_config(root)
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
            fake_client = FakeResponsesClient()
            fake_broker = FakeRabbitMQBroker()
            app = RelayApp(
                config,
                build_logger(),
                responses_client=fake_client,
                response_extractor=ResponseExtractor(),
            )
            app._rabbitmq_broker = fake_broker
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="msg-rmq-003")

            app.poll_once()
            fake_broker.reply_queue.append(
                FakeBrokerDelivery(
                    {
                        "schemaVersion": "relay-reply/v1",
                        "status": "error",
                        "conversationId": "CONV-20260307-001",
                        "messageId": "msg-rmq-003",
                        "fromGateway": "OptionDEF002",
                        "toGateway": "OptionABC001",
                        "returnSessionKey": build_relay_session_key(
                            to_gateway="OptionDEF002",
                            conversation_id="CONV-20260307-001",
                        ),
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                        "errorText": "worker failed",
                    }
                )
            )

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            message = app.store.get_message_by_turn_id("msg-rmq-003")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "FAILED_B")
            self.assertEqual(message["last_error"], "worker failed")
            self.assertEqual(len(fake_broker.published_messages), 1)

    def test_custom_return_session_key_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            source = config.relay.watch_dir / "message.json"
            payload = {
                "schemaVersion": MESSAGE_SCHEMA_VERSION,
                "conversationId": "CONV-CUSTOM-001",
                "messageId": "MSG-CUSTOM-001",
                "fromGateway": "OptionABC001",
                "toGateway": "OptionDEF002",
                "body": "custom return session",
                "returnSessionKey": "relay-manual/custom",
                "idempotencyKey": "MSG-CUSTOM-001",
                "ttlSeconds": 300,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            source.write_text(json.dumps(payload), encoding="utf-8")
            app = RelayApp(config, build_logger())
            app.initialize()

            processed = app.poll_once()

            self.assertEqual(processed, 1)
            envelope = Envelope.from_file(
                config.relay.processing_dir / "message.json",
                expected_schema_version=config.behavior.schema_version,
            )
            self.assertEqual(envelope.return_session_key, "relay-manual/custom")

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

    def test_a_inject_timeout_is_marked_done_when_session_log_confirms_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            client = InjectTimeoutResponsesClient()
            session_fetcher = SequenceSessionFetcher([True])
            app = RelayApp(
                config,
                build_logger(),
                responses_client=client,
                response_extractor=ResponseExtractor(),
                remote_session_fetcher=session_fetcher,
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="MSG-TIMEOUT-CONFIRMED")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ), mock.patch.object(app, "_session_monitor_connection", return_value=object()):
                processed = app.poll_once()

            self.assertEqual(processed, 3)
            message = app.store.get_message_by_idempotency_key("MSG-TIMEOUT-CONFIRMED")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")
            self.assertEqual(message["reply_text"], "ACK")
            attempts = app.store.list_attempts(message_id=message["id"])
            self.assertEqual(
                [(row["target"], row["attempt_no"], row["result"]) for row in attempts],
                [
                    ("B", 1, "SUCCESS"),
                    ("A", 1, "SUCCESS"),
                ],
            )
            self.assertEqual(len(session_fetcher.lookups), 1)
            self.assertEqual(
                session_fetcher.lookups[0][0],
                DEFAULT_HUMAN_SESSION_KEY,
            )
            self.assertEqual(
                [call[0] for call in client.calls],
                ["OptionDEF002", "OptionABC001"],
            )

    def test_failed_a_injection_retry_confirms_delivery_without_second_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                build_config(root),
                retry=replace(
                    build_config(root).retry,
                    initial_backoff_ms=1,
                    max_backoff_ms=1,
                    jitter=False,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            client = InjectTimeoutResponsesClient()
            session_fetcher = SequenceSessionFetcher([False, True])
            app = RelayApp(
                config,
                build_logger(),
                responses_client=client,
                response_extractor=ResponseExtractor(),
                remote_session_fetcher=session_fetcher,
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_message(source, message_id="MSG-TIMEOUT-RETRY")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ), mock.patch.object(app, "_session_monitor_connection", return_value=object()):
                first_processed = app.poll_once()
                time.sleep(0.01)
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 2)
            self.assertEqual(second_processed, 1)
            message = app.store.get_message_by_idempotency_key("MSG-TIMEOUT-RETRY")
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["status"], "DONE")
            attempts = app.store.list_attempts(message_id=message["id"])
            self.assertEqual(
                [(row["target"], row["attempt_no"], row["result"]) for row in attempts],
                [
                    ("B", 1, "SUCCESS"),
                    ("A", 1, "FAILED"),
                    ("A", 2, "SUCCESS"),
                ],
            )
            self.assertEqual(
                [call[0] for call in client.calls],
                ["OptionDEF002", "OptionABC001"],
            )
            self.assertEqual(len(session_fetcher.lookups), 2)

    def test_b_dispatch_timeout_is_confirmed_from_worker_session_and_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = replace(
                build_config(root),
                endpoint_b=replace(
                    build_config(root).endpoint_b,
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
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            client = DispatchTimeoutResponsesClient()
            session_fetcher = WorkerReplySessionFetcher(
                [
                    {
                        "found": True,
                        "sessionFile": "worker-main.jsonl",
                        "requestAt": "2026-03-09T11:16:34Z",
                        "replyAt": "2026-03-09T11:17:18Z",
                        "replyText": "analysis-ok",
                    }
                ]
            )
            app = RelayApp(
                config,
                build_logger(),
                responses_client=client,
                response_extractor=ResponseExtractor(),
                remote_session_fetcher=session_fetcher,
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_minimal_message(source, body="long macro instruction")

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
            message = app.store.list_messages()[0]
            self.assertEqual(message["status"], "DONE")
            self.assertEqual(message["reply_text"], "analysis-ok")
            attempts = app.store.list_attempts(message_id=message["id"])
            self.assertEqual(
                [(row["target"], row["attempt_no"], row["result"]) for row in attempts],
                [
                    ("B", 1, "SUCCESS"),
                    ("A", 1, "SUCCESS"),
                ],
            )
            self.assertEqual([call[0] for call in client.calls], ["OptionDEF002", "OptionABC001"])
            self.assertEqual(len(session_fetcher.reply_lookups), 1)

    def test_b_dispatch_waits_for_worker_reply_without_resending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_config = build_config(root)
            config = replace(
                base_config,
                endpoint_b=replace(
                    base_config.endpoint_b,
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
                    base_config.retry,
                    initial_backoff_ms=1,
                    max_backoff_ms=1,
                    jitter=False,
                ),
            )
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            client = DispatchTimeoutResponsesClient()
            session_fetcher = WorkerReplySessionFetcher(
                [
                    {
                        "found": True,
                        "sessionFile": "worker-main.jsonl",
                        "requestAt": "2026-03-09T11:16:34Z",
                    },
                    {
                        "found": True,
                        "sessionFile": "worker-main.jsonl",
                        "requestAt": "2026-03-09T11:16:34Z",
                        "replyAt": "2026-03-09T11:17:18Z",
                        "replyText": "analysis-ok",
                    },
                ]
            )
            app = RelayApp(
                config,
                build_logger(),
                responses_client=client,
                response_extractor=ResponseExtractor(),
                remote_session_fetcher=session_fetcher,
            )
            app.initialize()

            source = config.relay.watch_dir / "message.json"
            write_minimal_message(source, body="long macro instruction")

            with mock.patch.dict(
                os.environ,
                {
                    "A_GATEWAY_TOKEN": "dummy-a",
                    "B_GATEWAY_TOKEN": "dummy-b",
                },
                clear=False,
            ):
                first_processed = app.poll_once()
                time.sleep(1.05)
                second_processed = app.poll_once()

            self.assertEqual(first_processed, 1)
            self.assertEqual(second_processed, 2)
            message = app.store.list_messages()[0]
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
            self.assertEqual([call[0] for call in client.calls], ["OptionDEF002", "OptionABC001"])
            self.assertEqual(len(session_fetcher.reply_lookups), 2)


if __name__ == "__main__":
    unittest.main()
