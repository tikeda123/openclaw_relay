from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from openclaw_relay.config import AppConfig
from openclaw_relay.rabbitmq_runtime import RabbitMQReplyConsumer, RabbitMQWorkerAdapter
from openclaw_relay.responses_client import ResponsesClientError


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
            default_session_key = "agent:main:main"
            token_env = "A_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [workers.b]
            display_name = "OptionDEF002"
            base_url = "http://127.0.0.1:31901"
            agent_id = "main"
            default_session_key = "agent:main:main"
            token_env = "B_GATEWAY_TOKEN"
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
            schema_version = "relay-message/v1"
            default_ttl_seconds = 300
            duplicate_policy = "suppress"
            inject_notice_on_error = true

            [rabbitmq]
            host = "127.0.0.1"
            port = 5672
            virtual_host = "/openclaw-relay"
            user_env = "RABBITMQ_USER"
            password_env = "RABBITMQ_PASSWORD"
            heartbeat_seconds = 30
            blocked_connection_timeout_seconds = 30
            prefetch_count = 4
            queue_type = "quorum"
            dispatch_exchange = "relay.dispatch.direct"
            reply_exchange = "relay.reply.direct"
            deadletter_exchange = "relay.dead.direct"
            events_exchange = "relay.events.topic"
            worker_queue_prefix = "relay.worker"
            control_queue_prefix = "relay.control"
            deadletter_queue_prefix = "relay.deadletter"
            """
        ).strip(),
        encoding="utf-8",
    )


class FakeDelivery:
    def __init__(self, body: dict[str, object]):
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.acked = False
        self.nacked = None

    def ack(self) -> None:
        self.acked = True

    def nack(self, *, requeue: bool) -> None:
        self.nacked = requeue


class FakeBroker:
    def __init__(self, delivery=None):
        self.delivery = delivery
        self.published_replies: list[dict[str, object]] = []
        self.ensure_topology_calls = 0

    def ensure_topology(self):
        self.ensure_topology_calls += 1
        return None

    def worker_inbox_binding(self, _worker):
        return type("Binding", (), {"queue": "relay.worker.optiondef002.inbox"})

    def relay_reply_binding(self):
        return type("Binding", (), {"queue": "relay.control.optionabc001.reply"})

    def consume_one(self, _queue_name):
        delivery = self.delivery
        self.delivery = None
        return delivery

    def publish_reply_message(self, payload: dict[str, object]):
        self.published_replies.append(payload)
        return payload


class FakeResponsesClient:
    def __init__(self, response=None, exc: Exception | None = None):
        self.response = response
        self.exc = exc
        self.calls: list[dict[str, object]] = []

    def send_user_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


class RabbitMQRuntimeTests(unittest.TestCase):
    def _config(self) -> AppConfig:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "relay.toml"
        write_config(path)
        return AppConfig.from_file(path)

    def test_worker_adapter_processes_message_and_publishes_reply(self) -> None:
        config = self._config()
        delivery = FakeDelivery(
            {
                "toGateway": "OptionDEF002",
                "body": "Reply with ACK",
                "conversationId": "CONV-001",
                "messageId": "MSG-001",
                "fromGateway": "OptionABC001",
            }
        )
        broker = FakeBroker(delivery)
        responses_client = FakeResponsesClient(
            response={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ACK"}],
                    }
                ]
            }
        )
        logger = logging.getLogger("test-worker-adapter")

        adapter = RabbitMQWorkerAdapter(
            config,
            logger,
            worker_name="OptionDEF002",
            broker=broker,
            responses_client=responses_client,
        )

        with patch.dict(os.environ, {"B_GATEWAY_TOKEN": "token-b"}, clear=False):
            handled = adapter.process_one()

        self.assertTrue(handled)
        self.assertTrue(delivery.acked)
        self.assertEqual(len(broker.published_replies), 1)
        self.assertEqual(broker.published_replies[0]["status"], "ok")
        self.assertEqual(broker.published_replies[0]["replyText"], "ACK")

    def test_worker_adapter_run_ensures_topology_before_polling(self) -> None:
        config = self._config()
        broker = FakeBroker()
        logger = logging.getLogger("test-worker-adapter-run")

        adapter = RabbitMQWorkerAdapter(
            config,
            logger,
            worker_name="OptionDEF002",
            broker=broker,
        )

        processed = adapter.run(once=True)

        self.assertEqual(processed, 0)
        self.assertEqual(broker.ensure_topology_calls, 1)

    def test_worker_adapter_timeout_acks_without_publishing_error_reply(self) -> None:
        config = self._config()
        delivery = FakeDelivery(
            {
                "toGateway": "OptionDEF002",
                "body": "do work",
                "conversationId": "CONV-002",
                "messageId": "MSG-002",
                "fromGateway": "OptionABC001",
            }
        )
        broker = FakeBroker(delivery)
        responses_client = FakeResponsesClient(
            exc=ResponsesClientError("OptionDEF002 request failed: timed out")
        )
        logger = logging.getLogger("test-worker-adapter-error")

        adapter = RabbitMQWorkerAdapter(
            config,
            logger,
            worker_name="OptionDEF002",
            broker=broker,
            responses_client=responses_client,
        )

        with patch.dict(os.environ, {"B_GATEWAY_TOKEN": "token-b"}, clear=False):
            handled = adapter.process_one()

        self.assertTrue(handled)
        self.assertTrue(delivery.acked)
        self.assertEqual(broker.published_replies, [])

    def test_reply_consumer_injects_reply_into_control_gateway(self) -> None:
        config = self._config()
        delivery = FakeDelivery(
            {
                "schemaVersion": "relay-reply/v1",
                "status": "ok",
                "conversationId": "CONV-003",
                "messageId": "MSG-003",
                "fromGateway": "OptionDEF002",
                "toGateway": "OptionABC001",
                "returnSessionKey": "relay:optiondef002:conv-003",
                "createdAt": "2026-03-09T22:00:00+09:00",
                "replyText": "ACK",
            }
        )
        broker = FakeBroker(delivery)
        responses_client = FakeResponsesClient(response={"output": []})
        logger = logging.getLogger("test-reply-consumer")

        consumer = RabbitMQReplyConsumer(
            config,
            logger,
            broker=broker,
            responses_client=responses_client,
        )

        with patch.dict(os.environ, {"A_GATEWAY_TOKEN": "token-a"}, clear=False):
            handled = consumer.process_one()

        self.assertTrue(handled)
        self.assertTrue(delivery.acked)
        self.assertEqual(len(responses_client.calls), 1)
        self.assertEqual(
            responses_client.calls[0]["session_key"],
            "relay:optiondef002:conv-003",
        )
        self.assertIn("ACK", responses_client.calls[0]["content"])

    def test_reply_consumer_run_ensures_topology_before_polling(self) -> None:
        config = self._config()
        broker = FakeBroker()
        logger = logging.getLogger("test-reply-consumer-run")

        consumer = RabbitMQReplyConsumer(
            config,
            logger,
            broker=broker,
        )

        processed = consumer.run(once=True)

        self.assertEqual(processed, 0)
        self.assertEqual(broker.ensure_topology_calls, 1)


if __name__ == "__main__":
    unittest.main()
