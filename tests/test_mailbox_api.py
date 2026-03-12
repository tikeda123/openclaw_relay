from __future__ import annotations

from pathlib import Path
import json
import logging
import tempfile
import unittest

from openclaw_relay.app import RelayApp
from openclaw_relay.config import (
    AppConfig,
    AuditConfig,
    BehaviorConfig,
    EndpointConfig,
    RabbitMQConfig,
    RelayConfig,
    RetryConfig,
    SecurityConfig,
)
from openclaw_relay.envelope import MESSAGE_SCHEMA_VERSION


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
            agent_id="main",
            default_session_key="main",
            token_env="A_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        endpoint_b=EndpointConfig(
            name="b",
            display_name="OptionDEF002",
            base_url="http://127.0.0.1:31901",
            agent_id="main",
            default_session_key="main",
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
            allow_http_only_for_localhost=True,
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
    config = build_config(root)
    return AppConfig(
        relay=config.relay,
        endpoint_a=config.endpoint_a,
        endpoint_b=config.endpoint_b,
        retry=config.retry,
        security=config.security,
        audit=config.audit,
        behavior=config.behavior,
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
        workers=config.workers,
        default_worker_name=config.default_worker_name,
        transport_mode="rabbitmq",
    )


def build_logger() -> logging.Logger:
    logger = logging.getLogger("openclaw_relay_mailbox_test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


class FakeMailboxDelivery:
    def __init__(self, payload: dict[str, object]) -> None:
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.acked = False
        self.nacked = None

    def ack(self) -> None:
        self.acked = True

    def nack(self, *, requeue: bool) -> None:
        self.nacked = requeue


class FakeMailboxBroker:
    def __init__(self) -> None:
        self.queues: dict[str, list[dict[str, object]]] = {}
        self.ensure_topology_calls = 0

    def ensure_topology(self):
        self.ensure_topology_calls += 1
        return None

    def publish_mailbox_message(self, payload: dict[str, object]) -> dict[str, object]:
        mailbox = str(payload["to"])
        self.queues.setdefault(mailbox, []).append(dict(payload))
        return {
            "exchange": "relay.mailbox.direct",
            "routingKey": mailbox.lower(),
            "queue": f"relay.mailbox.{mailbox.lower()}.inbox",
            "messageId": payload["messageId"],
            "conversationId": payload["conversationId"],
            "from": payload["from"],
            "to": payload["to"],
        }

    def consume_mailbox_message(self, mailbox: str):
        queue = self.queues.get(mailbox, [])
        if not queue:
            return None
        return FakeMailboxDelivery(queue.pop(0))


class MailboxApiTests(unittest.TestCase):
    def test_put_and_get_mailbox_message_flow_is_fifo_and_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = RelayApp(build_config(root), build_logger())
            app.initialize()

            first = app.put_mailbox_message(
                {
                    "from": "OptionABC001",
                    "to": "OptionDEF002",
                    "body": "first",
                }
            )
            second = app.put_mailbox_message(
                {
                    "from": "OptionABC001",
                    "to": "OptionDEF002",
                    "body": "second",
                }
            )

            received_first = app.get_mailbox_message("OptionDEF002")
            received_second = app.get_mailbox_message("OptionDEF002")
            received_third = app.get_mailbox_message("OptionDEF002")

            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "queued")
            self.assertIsNotNone(received_first)
            self.assertIsNotNone(received_second)
            assert received_first is not None
            assert received_second is not None
            self.assertEqual(received_first["body"], "first")
            self.assertEqual(received_second["body"], "second")
            self.assertEqual(received_first["status"], "delivered")
            self.assertEqual(received_second["status"], "delivered")
            self.assertIn("dequeuedAt", received_first)
            self.assertIn("dequeuedAt", received_second)
            self.assertIsNone(received_third)

    def test_put_mailbox_message_preserves_conversation_and_reply_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = RelayApp(build_config(root), build_logger())
            app.initialize()

            queued = app.put_mailbox_message(
                {
                    "fromGateway": "OptionDEF002",
                    "toGateway": "OptionABC001",
                    "body": "reply",
                    "conversationId": "conv-123",
                    "inReplyTo": "msg-001",
                    "messageId": "msg-002",
                    "notifyHuman": True,
                }
            )
            received = app.get_mailbox_message("OptionABC001")

            self.assertEqual(queued["messageId"], "msg-002")
            self.assertEqual(queued["conversationId"], "conv-123")
            self.assertEqual(queued["inReplyTo"], "msg-001")
            self.assertIs(queued["notifyHuman"], True)
            self.assertIsNotNone(received)
            assert received is not None
            self.assertEqual(received["messageId"], "msg-002")
            self.assertEqual(received["conversationId"], "conv-123")
            self.assertEqual(received["inReplyTo"], "msg-001")
            self.assertEqual(received["from"], "OptionDEF002")
            self.assertEqual(received["to"], "OptionABC001")
            self.assertIs(received["notifyHuman"], True)

    def test_rabbitmq_backend_keeps_fifo_and_destructive_get(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            broker = FakeMailboxBroker()
            app = RelayApp(
                build_rabbitmq_config(root),
                build_logger(),
                rabbitmq_broker=broker,
            )
            app.initialize()

            self.assertEqual(broker.ensure_topology_calls, 1)

            first = app.put_mailbox_message(
                {
                    "from": "OptionABC001",
                    "to": "OptionDEF002",
                    "body": "first",
                    "notifyHuman": True,
                }
            )
            second = app.put_mailbox_message(
                {
                    "from": "OptionABC001",
                    "to": "OptionDEF002",
                    "body": "second",
                }
            )

            received_first = app.get_mailbox_message("OptionDEF002")
            received_second = app.get_mailbox_message("OptionDEF002")
            received_third = app.get_mailbox_message("OptionDEF002")

            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "queued")
            self.assertIs(first["notifyHuman"], True)
            self.assertIs(second["notifyHuman"], False)
            self.assertIsNotNone(received_first)
            self.assertIsNotNone(received_second)
            assert received_first is not None
            assert received_second is not None
            self.assertEqual(received_first["body"], "first")
            self.assertEqual(received_second["body"], "second")
            self.assertEqual(received_first["status"], "delivered")
            self.assertEqual(received_second["status"], "delivered")
            self.assertIs(received_first["notifyHuman"], True)
            self.assertIs(received_second["notifyHuman"], False)
            self.assertIsNone(received_third)
            self.assertEqual(broker.queues["OptionDEF002"], [])


if __name__ == "__main__":
    unittest.main()
