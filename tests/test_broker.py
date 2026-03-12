from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

from openclaw_relay.broker import RabbitMQBroker
from openclaw_relay.config import AppConfig


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

            [workers.def002]
            display_name = "OptionDEF002"
            base_url = "http://127.0.0.1:31901"
            agent_id = "main"
            default_session_key = "agent:main:main"
            token_env = "B_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [workers.xyz003]
            display_name = "OptionXYZ003"
            base_url = "http://127.0.0.1:31902"
            agent_id = "main"
            default_session_key = "agent:main:main"
            token_env = "C_GATEWAY_TOKEN"
            timeout_seconds = 30.0

            [routing]
            default_worker = "def002"

            [retry]
            max_attempts_b = 5
            max_attempts_a = 10
            initial_backoff_ms = 1000
            max_backoff_ms = 30000
            jitter = true

            [security]
            require_private_ingress = true
            allow_http_only_for_localhost = false
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
            host = "rabbitmq.internal"
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


class FakeChannel:
    def __init__(self) -> None:
        self.exchanges: list[tuple[str, str, bool]] = []
        self.queues: list[tuple[str, bool, dict[str, str] | None]] = []
        self.bindings: list[tuple[str, str, str]] = []
        self.confirmed = False
        self.published: list[dict[str, object]] = []
        self.next_message = None
        self.acked: list[int] = []
        self.nacked: list[tuple[int, bool]] = []

    def exchange_declare(self, *, exchange: str, exchange_type: str, durable: bool) -> None:
        self.exchanges.append((exchange, exchange_type, durable))

    def queue_declare(
        self,
        *,
        queue: str,
        durable: bool,
        arguments: dict[str, str] | None = None,
    ) -> None:
        self.queues.append((queue, durable, arguments))

    def queue_bind(self, *, queue: str, exchange: str, routing_key: str) -> None:
        self.bindings.append((queue, exchange, routing_key))

    def confirm_delivery(self) -> None:
        self.confirmed = True

    def basic_publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        body: bytes,
        properties,
        mandatory: bool,
    ) -> bool:
        self.published.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
                "mandatory": mandatory,
            }
        )
        return True

    def basic_get(self, *, queue: str, auto_ack: bool):
        if self.next_message is None:
            return None, None, None
        message = self.next_message
        self.next_message = None
        return (
            types.SimpleNamespace(delivery_tag=message["delivery_tag"]),
            message["properties"],
            message["body"],
        )

    def basic_ack(self, delivery_tag: int) -> None:
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        self.nacked.append((delivery_tag, requeue))


class FakeConnection:
    def __init__(self) -> None:
        self.channel_obj = FakeChannel()
        self.server_properties = {"product": "RabbitMQ", "version": "4.1.0"}
        self.closed = False

    def channel(self) -> FakeChannel:
        return self.channel_obj

    def close(self) -> None:
        self.closed = True


class FakePikaModule:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.captured_credentials = None
        self.captured_parameters = None

    def PlainCredentials(self, username: str, password: str) -> tuple[str, str]:
        self.captured_credentials = (username, password)
        return (username, password)

    def ConnectionParameters(self, **kwargs):
        self.captured_parameters = types.SimpleNamespace(**kwargs)
        return self.captured_parameters

    def BlockingConnection(self, _parameters) -> FakeConnection:
        return self.connection

    def BasicProperties(self, **kwargs):
        return types.SimpleNamespace(**kwargs)


class BrokerTests(unittest.TestCase):
    def test_describe_topology_uses_worker_display_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)

            plan = RabbitMQBroker(config).describe_topology().as_dict()

            self.assertEqual(
                plan["relayReplyQueue"]["queue"],
                "relay.control.optionabc001.reply",
            )
            self.assertEqual(plan["mailboxExchange"], "relay.mailbox.direct")
            self.assertEqual(
                [item["queue"] for item in plan["workerInboxQueues"]],
                [
                    "relay.worker.optiondef002.inbox",
                    "relay.worker.optionxyz003.inbox",
                ],
            )

    def test_ensure_topology_declares_exchanges_and_quorum_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            assert config.rabbitmq is not None
            fake_connection = FakeConnection()
            fake_pika = FakePikaModule(fake_connection)

            with patch.dict(
                "os.environ",
                {"RABBITMQ_USER": "relay-user", "RABBITMQ_PASSWORD": "relay-pass"},
                clear=False,
            ):
                with patch("openclaw_relay.broker._load_pika", return_value=fake_pika):
                    RabbitMQBroker(config).ensure_topology()

            self.assertIn(("relay.dispatch.direct", "direct", True), fake_connection.channel_obj.exchanges)
            self.assertIn(("relay.reply.direct", "direct", True), fake_connection.channel_obj.exchanges)
            self.assertIn(("relay.mailbox.direct", "direct", True), fake_connection.channel_obj.exchanges)
            queue_names = [entry[0] for entry in fake_connection.channel_obj.queues]
            self.assertIn("relay.control.optionabc001.reply", queue_names)
            self.assertIn("relay.worker.optiondef002.inbox", queue_names)
            self.assertIn("relay.deadletter.optiondef002.worker", queue_names)
            self.assertEqual(
                fake_pika.captured_credentials,
                ("relay-user", "relay-pass"),
            )
            self.assertEqual(fake_pika.captured_parameters.host, "rabbitmq.internal")

    def test_publish_message_uses_confirmed_delivery_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            fake_connection = FakeConnection()
            fake_pika = FakePikaModule(fake_connection)
            payload = {
                "schemaVersion": "relay-message/v1",
                "conversationId": "CONV-001",
                "messageId": "MSG-001",
                "fromGateway": "OptionABC001",
                "toGateway": "OptionDEF002",
                "body": "hello",
            }

            with patch.dict(
                "os.environ",
                {"RABBITMQ_USER": "relay-user", "RABBITMQ_PASSWORD": "relay-pass"},
                clear=False,
            ):
                with patch("openclaw_relay.broker._load_pika", return_value=fake_pika):
                    summary = RabbitMQBroker(config).publish_message(payload)

            self.assertTrue(fake_connection.channel_obj.confirmed)
            published = fake_connection.channel_obj.published[0]
            self.assertEqual(published["exchange"], "relay.dispatch.direct")
            self.assertEqual(published["routing_key"], "optiondef002")
            self.assertEqual(published["properties"].message_id, "MSG-001")
            self.assertEqual(
                published["properties"].reply_to,
                "relay.control.optionabc001.reply",
            )
            self.assertEqual(summary["routingKey"], "optiondef002")

    def test_publish_and_consume_mailbox_message_use_mailbox_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            fake_connection = FakeConnection()
            fake_pika = FakePikaModule(fake_connection)
            payload = {
                "messageId": "MSG-MBX-001",
                "conversationId": "CONV-MBX-001",
                "from": "OptionABC001",
                "to": "OptionDEF002",
                "body": "hello",
                "queuedAt": "2026-03-11T13:40:00+00:00",
            }

            with patch.dict(
                "os.environ",
                {"RABBITMQ_USER": "relay-user", "RABBITMQ_PASSWORD": "relay-pass"},
                clear=False,
            ):
                with patch("openclaw_relay.broker._load_pika", return_value=fake_pika):
                    broker = RabbitMQBroker(config)
                    summary = broker.publish_mailbox_message(payload)

                    fake_connection.channel_obj.next_message = {
                        "delivery_tag": 9,
                        "properties": types.SimpleNamespace(message_id="MSG-MBX-001"),
                        "body": b'{"messageId":"MSG-MBX-001"}',
                    }
                    delivery = broker.consume_mailbox_message("OptionDEF002")

            self.assertEqual(summary["exchange"], "relay.mailbox.direct")
            self.assertEqual(summary["queue"], "relay.mailbox.optiondef002.inbox")
            published = fake_connection.channel_obj.published[0]
            self.assertEqual(published["exchange"], "relay.mailbox.direct")
            self.assertEqual(published["routing_key"], "optiondef002")
            self.assertEqual(published["properties"].type, "relay-mailbox-message/v1")
            queue_names = [entry[0] for entry in fake_connection.channel_obj.queues]
            self.assertIn("relay.mailbox.optiondef002.inbox", queue_names)
            assert delivery is not None
            self.assertEqual(delivery.queue, "relay.mailbox.optiondef002.inbox")

    def test_consume_one_and_publish_reply_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            write_config(config_path)
            config = AppConfig.from_file(config_path)
            fake_connection = FakeConnection()
            fake_pika = FakePikaModule(fake_connection)
            fake_connection.channel_obj.next_message = {
                "delivery_tag": 7,
                "properties": types.SimpleNamespace(message_id="MSG-001"),
                "body": b'{"schemaVersion":"relay-message/v1"}',
            }

            with patch.dict(
                "os.environ",
                {"RABBITMQ_USER": "relay-user", "RABBITMQ_PASSWORD": "relay-pass"},
                clear=False,
            ):
                with patch("openclaw_relay.broker._load_pika", return_value=fake_pika):
                    broker = RabbitMQBroker(config)
                    delivery = broker.consume_one("relay.worker.optiondef002.inbox")
                    assert delivery is not None
                    self.assertEqual(delivery.delivery_tag, 7)
                    delivery.ack()
                    summary = broker.publish_reply_message(
                        {
                            "schemaVersion": "relay-reply/v1",
                            "status": "ok",
                            "conversationId": "CONV-001",
                            "messageId": "MSG-001",
                            "fromGateway": "OptionDEF002",
                            "toGateway": "OptionABC001",
                            "replyText": "ACK",
                        }
                    )

            self.assertEqual(fake_connection.channel_obj.acked, [7])
            self.assertEqual(summary["queue"], "relay.control.optionabc001.reply")


if __name__ == "__main__":
    unittest.main()
