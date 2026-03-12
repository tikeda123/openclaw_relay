from __future__ import annotations

from dataclasses import dataclass
import json
import re
import ssl
from typing import Any

from openclaw_relay.config import AppConfig, ConfigError, EndpointConfig, RabbitMQConfig


class RabbitMQError(RuntimeError):
    """Raised when RabbitMQ operations fail."""


@dataclass(frozen=True)
class QueueBinding:
    queue: str
    exchange: str
    routing_key: str
    queue_type: str


@dataclass
class RabbitMQDelivery:
    connection: Any
    channel: Any
    delivery_tag: int
    body: bytes
    properties: Any
    queue: str

    def ack(self) -> None:
        self.channel.basic_ack(self.delivery_tag)
        self.connection.close()

    def nack(self, *, requeue: bool) -> None:
        self.channel.basic_nack(self.delivery_tag, requeue=requeue)
        self.connection.close()


@dataclass(frozen=True)
class RabbitMQTopologyPlan:
    dispatch_exchange: str
    reply_exchange: str
    mailbox_exchange: str
    deadletter_exchange: str
    events_exchange: str
    relay_reply_queue: QueueBinding
    relay_deadletter_queue: QueueBinding
    worker_inbox_queues: tuple[QueueBinding, ...]
    worker_deadletter_queues: tuple[QueueBinding, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dispatchExchange": self.dispatch_exchange,
            "replyExchange": self.reply_exchange,
            "mailboxExchange": self.mailbox_exchange,
            "deadletterExchange": self.deadletter_exchange,
            "eventsExchange": self.events_exchange,
            "relayReplyQueue": _binding_dict(self.relay_reply_queue),
            "relayDeadletterQueue": _binding_dict(self.relay_deadletter_queue),
            "workerInboxQueues": [_binding_dict(binding) for binding in self.worker_inbox_queues],
            "workerDeadletterQueues": [
                _binding_dict(binding) for binding in self.worker_deadletter_queues
            ],
        }


def _binding_dict(binding: QueueBinding) -> dict[str, str]:
    return {
        "queue": binding.queue,
        "exchange": binding.exchange,
        "routingKey": binding.routing_key,
        "queueType": binding.queue_type,
    }


def _load_pika() -> Any:
    try:
        import pika  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised by CLI tests
        raise RabbitMQError(
            "RabbitMQ support requires the 'pika' package. Install project dependencies first."
        ) from exc
    return pika


def _normalize_segment(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "default"


class RabbitMQBroker:
    def __init__(self, config: AppConfig):
        if config.rabbitmq is None:
            raise ConfigError("RabbitMQ is not configured in this relay config")
        self.app_config = config
        self.config = config.rabbitmq

    def describe_topology(self) -> RabbitMQTopologyPlan:
        rabbitmq = self.config
        control_name = _normalize_segment(self.app_config.endpoint_a.display_name)
        relay_reply_queue = QueueBinding(
            queue=f"{rabbitmq.control_queue_prefix}.{control_name}.reply",
            exchange=rabbitmq.reply_exchange,
            routing_key=control_name,
            queue_type=rabbitmq.queue_type,
        )
        relay_deadletter_queue = QueueBinding(
            queue=f"{rabbitmq.deadletter_queue_prefix}.{control_name}.control",
            exchange=rabbitmq.deadletter_exchange,
            routing_key=control_name,
            queue_type=rabbitmq.queue_type,
        )
        worker_inbox_queues: list[QueueBinding] = []
        worker_deadletter_queues: list[QueueBinding] = []
        for worker in self.app_config.worker_endpoints():
            worker_name = _normalize_segment(worker.display_name)
            worker_inbox_queues.append(
                QueueBinding(
                    queue=f"{rabbitmq.worker_queue_prefix}.{worker_name}.inbox",
                    exchange=rabbitmq.dispatch_exchange,
                    routing_key=worker_name,
                    queue_type=rabbitmq.queue_type,
                )
            )
            worker_deadletter_queues.append(
                QueueBinding(
                    queue=f"{rabbitmq.deadletter_queue_prefix}.{worker_name}.worker",
                    exchange=rabbitmq.deadletter_exchange,
                    routing_key=worker_name,
                    queue_type=rabbitmq.queue_type,
                )
            )
        return RabbitMQTopologyPlan(
            dispatch_exchange=rabbitmq.dispatch_exchange,
            reply_exchange=rabbitmq.reply_exchange,
            mailbox_exchange=rabbitmq.mailbox_exchange,
            deadletter_exchange=rabbitmq.deadletter_exchange,
            events_exchange=rabbitmq.events_exchange,
            relay_reply_queue=relay_reply_queue,
            relay_deadletter_queue=relay_deadletter_queue,
            worker_inbox_queues=tuple(worker_inbox_queues),
            worker_deadletter_queues=tuple(worker_deadletter_queues),
        )

    def relay_reply_binding(self) -> QueueBinding:
        return self.describe_topology().relay_reply_queue

    def mailbox_binding(self, mailbox: str) -> QueueBinding:
        normalized = _normalize_segment(mailbox)
        return QueueBinding(
            queue=f"{self.config.mailbox_queue_prefix}.{normalized}.inbox",
            exchange=self.config.mailbox_exchange,
            routing_key=normalized,
            queue_type=self.config.queue_type,
        )

    def worker_inbox_binding(self, worker: EndpointConfig | str) -> QueueBinding:
        worker_name = worker.name if isinstance(worker, EndpointConfig) else worker
        for binding in self.describe_topology().worker_inbox_queues:
            if binding.queue.endswith(f".{_normalize_segment(worker_name)}.inbox"):
                return binding
        resolved = self.app_config.resolve_worker(worker_name)
        normalized = _normalize_segment(resolved.display_name)
        for binding in self.describe_topology().worker_inbox_queues:
            if binding.routing_key == normalized:
                return binding
        raise RabbitMQError(f"worker inbox queue not found for {worker_name!r}")

    def check_connection(self) -> dict[str, object]:
        connection = self._open_connection()
        try:
            return {
                "host": self.config.host,
                "port": self.config.port,
                "virtualHost": self.config.virtual_host,
                "heartbeatSeconds": self.config.heartbeat_seconds,
                "prefetchCount": self.config.prefetch_count,
                "queueType": self.config.queue_type,
                "serverProperties": dict(getattr(connection, "server_properties", {}) or {}),
            }
        finally:
            connection.close()

    def ensure_topology(self) -> RabbitMQTopologyPlan:
        plan = self.describe_topology()
        connection = self._open_connection()
        try:
            channel = connection.channel()
            self._declare_exchange(channel, plan.dispatch_exchange, "direct")
            self._declare_exchange(channel, plan.reply_exchange, "direct")
            self._declare_exchange(channel, plan.mailbox_exchange, "direct")
            self._declare_exchange(channel, plan.deadletter_exchange, "direct")
            self._declare_exchange(channel, plan.events_exchange, "topic")
            self._declare_queue(channel, plan.relay_reply_queue)
            self._declare_queue(channel, plan.relay_deadletter_queue)
            for binding in plan.worker_inbox_queues:
                self._declare_queue(channel, binding)
            for binding in plan.worker_deadletter_queues:
                self._declare_queue(channel, binding)
            return plan
        finally:
            connection.close()

    def publish_message(self, payload: dict[str, object]) -> dict[str, object]:
        worker = self.app_config.resolve_worker(str(payload["toGateway"]))
        plan = self.describe_topology()
        relay_reply_queue = plan.relay_reply_queue
        routing_key = _normalize_segment(worker.display_name)
        connection = self._open_connection()
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            properties = self._message_properties(payload, relay_reply_queue.queue)
            published = channel.basic_publish(
                exchange=self.config.dispatch_exchange,
                routing_key=routing_key,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                properties=properties,
                mandatory=True,
            )
            if published is False:
                raise RabbitMQError("broker did not confirm the published message")
            return {
                "exchange": self.config.dispatch_exchange,
                "routingKey": routing_key,
                "replyTo": relay_reply_queue.queue,
                "messageId": payload["messageId"],
                "conversationId": payload["conversationId"],
                "toGateway": payload["toGateway"],
            }
        except RabbitMQError:
            raise
        except Exception as exc:  # pragma: no cover - broker/runtime dependent
            raise RabbitMQError(f"failed to publish message: {exc}") from exc
        finally:
            connection.close()

    def publish_reply_message(self, payload: dict[str, object]) -> dict[str, object]:
        relay_reply_queue = self.relay_reply_binding()
        connection = self._open_connection()
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            properties = self._reply_properties(payload)
            published = channel.basic_publish(
                exchange=self.config.reply_exchange,
                routing_key=relay_reply_queue.routing_key,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                properties=properties,
                mandatory=True,
            )
            if published is False:
                raise RabbitMQError("broker did not confirm the published reply")
            return {
                "exchange": self.config.reply_exchange,
                "routingKey": relay_reply_queue.routing_key,
                "queue": relay_reply_queue.queue,
                "messageId": payload["messageId"],
                "conversationId": payload["conversationId"],
                "status": payload["status"],
            }
        except RabbitMQError:
            raise
        except Exception as exc:  # pragma: no cover - broker/runtime dependent
            raise RabbitMQError(f"failed to publish reply: {exc}") from exc
        finally:
            connection.close()

    def publish_mailbox_message(self, payload: dict[str, object]) -> dict[str, object]:
        binding = self.mailbox_binding(str(payload["to"]))
        connection = self._open_connection()
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            self._declare_exchange(channel, binding.exchange, "direct")
            self._declare_queue(channel, binding)
            properties = self._mailbox_properties(payload)
            published = channel.basic_publish(
                exchange=binding.exchange,
                routing_key=binding.routing_key,
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                properties=properties,
                mandatory=True,
            )
            if published is False:
                raise RabbitMQError("broker did not confirm the published mailbox message")
            return {
                "exchange": binding.exchange,
                "routingKey": binding.routing_key,
                "queue": binding.queue,
                "messageId": payload["messageId"],
                "conversationId": payload["conversationId"],
                "from": payload["from"],
                "to": payload["to"],
            }
        except RabbitMQError:
            raise
        except Exception as exc:  # pragma: no cover - broker/runtime dependent
            raise RabbitMQError(f"failed to publish mailbox message: {exc}") from exc
        finally:
            connection.close()

    def consume_mailbox_message(self, mailbox: str) -> RabbitMQDelivery | None:
        binding = self.mailbox_binding(mailbox)
        connection = self._open_connection()
        try:
            channel = connection.channel()
            self._declare_exchange(channel, binding.exchange, "direct")
            self._declare_queue(channel, binding)
            method, properties, body = channel.basic_get(queue=binding.queue, auto_ack=False)
        except Exception:
            connection.close()
            raise
        if method is None:
            connection.close()
            return None
        return RabbitMQDelivery(
            connection=connection,
            channel=channel,
            delivery_tag=method.delivery_tag,
            body=body,
            properties=properties,
            queue=binding.queue,
        )

    def consume_one(self, queue_name: str) -> RabbitMQDelivery | None:
        connection = self._open_connection()
        try:
            channel = connection.channel()
            method, properties, body = channel.basic_get(queue=queue_name, auto_ack=False)
        except Exception:
            connection.close()
            raise
        if method is None:
            connection.close()
            return None
        return RabbitMQDelivery(
            connection=connection,
            channel=channel,
            delivery_tag=method.delivery_tag,
            body=body,
            properties=properties,
            queue=queue_name,
        )

    def _declare_exchange(self, channel: Any, exchange: str, exchange_type: str) -> None:
        channel.exchange_declare(exchange=exchange, exchange_type=exchange_type, durable=True)

    def _declare_queue(self, channel: Any, binding: QueueBinding) -> None:
        arguments = None
        if binding.queue_type == "quorum":
            arguments = {"x-queue-type": "quorum"}
        channel.queue_declare(queue=binding.queue, durable=True, arguments=arguments)
        channel.queue_bind(
            queue=binding.queue,
            exchange=binding.exchange,
            routing_key=binding.routing_key,
        )

    def _message_properties(self, payload: dict[str, object], reply_to: str) -> Any:
        pika = _load_pika()
        headers = {
            "schemaVersion": str(payload.get("schemaVersion", "")),
            "fromGateway": str(payload.get("fromGateway", "")),
            "toGateway": str(payload.get("toGateway", "")),
        }
        intent = payload.get("intent")
        if intent is not None:
            headers["intent"] = str(intent)
        return pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=str(payload["messageId"]),
            correlation_id=str(payload["conversationId"]),
            reply_to=reply_to,
            headers=headers,
            type="relay-message/v1",
            timestamp=None,
        )

    def _reply_properties(self, payload: dict[str, object]) -> Any:
        pika = _load_pika()
        headers = {
            "schemaVersion": str(payload.get("schemaVersion", "")),
            "fromGateway": str(payload.get("fromGateway", "")),
            "toGateway": str(payload.get("toGateway", "")),
            "status": str(payload.get("status", "")),
        }
        return pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=str(payload["messageId"]),
            correlation_id=str(payload["conversationId"]),
            headers=headers,
            type="relay-reply/v1",
            timestamp=None,
        )

    def _mailbox_properties(self, payload: dict[str, object]) -> Any:
        pika = _load_pika()
        headers = {
            "from": str(payload.get("from", "")),
            "to": str(payload.get("to", "")),
        }
        if payload.get("inReplyTo") is not None:
            headers["inReplyTo"] = str(payload["inReplyTo"])
        return pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=str(payload["messageId"]),
            correlation_id=str(payload["conversationId"]),
            headers=headers,
            type="relay-mailbox-message/v1",
            timestamp=None,
        )

    def _open_connection(self) -> Any:
        pika = _load_pika()
        credentials = pika.PlainCredentials(
            self.config.resolve_username(),
            self.config.resolve_password(),
        )
        ssl_options = self._build_ssl_options(pika)
        parameters = pika.ConnectionParameters(
            host=self.config.host,
            port=self.config.port,
            virtual_host=self.config.virtual_host,
            credentials=credentials,
            heartbeat=self.config.heartbeat_seconds,
            blocked_connection_timeout=self.config.blocked_connection_timeout_seconds,
            ssl_options=ssl_options,
        )
        try:
            return pika.BlockingConnection(parameters)
        except Exception as exc:  # pragma: no cover - dependent on broker runtime
            raise RabbitMQError(f"failed to connect to RabbitMQ: {exc}") from exc

    def _build_ssl_options(self, pika: Any) -> Any:
        tls = self.config.tls
        if tls is None or not tls.enabled:
            return None
        context = ssl.create_default_context(
            cafile=str(tls.ca_path) if tls.ca_path is not None else None
        )
        if tls.cert_path is not None and tls.key_path is not None:
            context.load_cert_chain(str(tls.cert_path), str(tls.key_path))
        server_hostname = tls.server_hostname or self.config.host
        return pika.SSLOptions(context, server_hostname)
