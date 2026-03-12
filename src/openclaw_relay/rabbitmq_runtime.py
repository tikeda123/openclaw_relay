from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import time
from typing import Any

from openclaw_relay.broker import RabbitMQBroker, RabbitMQDelivery, RabbitMQError
from openclaw_relay.config import AppConfig, ConfigError, EndpointConfig
from openclaw_relay.envelope import Envelope, EnvelopeError
from openclaw_relay.remote import RemoteError, RemoteTokenResolver
from openclaw_relay.response_extractor import ResponseExtractor, ResponseExtractorError
from openclaw_relay.responses_client import ResponsesClient, ResponsesClientError

REPLY_SCHEMA_VERSION = "relay-reply/v1"


def _default_human_session_key(agent_id: str) -> str:
    normalized = (agent_id or "main").strip() or "main"
    return f"agent:{normalized}:{normalized}"


@dataclass(frozen=True)
class RabbitMQReply:
    schema_version: str
    status: str
    conversation_id: str
    message_id: str
    from_gateway: str
    to_gateway: str
    return_session_key: str
    created_at: str
    reply_text: str | None = None
    error_text: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "status": self.status,
            "conversationId": self.conversation_id,
            "messageId": self.message_id,
            "fromGateway": self.from_gateway,
            "toGateway": self.to_gateway,
            "returnSessionKey": self.return_session_key,
            "createdAt": self.created_at,
        }
        if self.reply_text is not None:
            payload["replyText"] = self.reply_text
        if self.error_text is not None:
            payload["errorText"] = self.error_text
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RabbitMQReply":
        if not isinstance(payload, dict):
            raise ValueError("reply payload must be an object")
        return cls(
            schema_version=str(payload.get("schemaVersion") or REPLY_SCHEMA_VERSION),
            status=str(payload.get("status") or "error"),
            conversation_id=str(payload["conversationId"]),
            message_id=str(payload["messageId"]),
            from_gateway=str(payload["fromGateway"]),
            to_gateway=str(payload["toGateway"]),
            return_session_key=str(payload["returnSessionKey"]),
            created_at=str(payload["createdAt"]),
            reply_text=str(payload["replyText"]) if payload.get("replyText") is not None else None,
            error_text=str(payload["errorText"]) if payload.get("errorText") is not None else None,
        )


class RabbitMQWorkerAdapter:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        *,
        worker_name: str,
        broker: RabbitMQBroker | None = None,
        responses_client: ResponsesClient | None = None,
        response_extractor: ResponseExtractor | None = None,
        remote_token_resolver: RemoteTokenResolver | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.worker_endpoint = config.resolve_worker(worker_name)
        self.broker = broker or RabbitMQBroker(config)
        self.responses_client = responses_client or ResponsesClient()
        self.response_extractor = response_extractor or ResponseExtractor()
        self.remote_token_resolver = remote_token_resolver or RemoteTokenResolver()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self, *, once: bool) -> int:
        _ensure_rabbitmq_topology_if_available(self.broker, self.logger)
        processed = 0
        while self._running:
            handled = self.process_one()
            if handled:
                processed += 1
            if once:
                return processed
            if not handled:
                time.sleep(1.0)
        return processed

    def process_one(self) -> bool:
        binding = self.broker.worker_inbox_binding(self.worker_endpoint)
        delivery = self.broker.consume_one(binding.queue)
        if delivery is None:
            return False

        raw_payload: dict[str, Any] | None = None
        envelope: Envelope | None = None
        try:
            raw_payload = json.loads(delivery.body.decode("utf-8"))
            envelope = Envelope.from_payload(
                raw_payload,
                expected_schema_version=self.config.behavior.schema_version,
                default_ttl_seconds=self.config.behavior.default_ttl_seconds,
                default_from_gateway=self.config.endpoint_a.display_name,
            )
            token = self._resolve_endpoint_token(self.worker_endpoint)
            response = self.responses_client.send_user_message(
                endpoint=self.worker_endpoint,
                session_key=self.worker_endpoint.default_session_key,
                content=envelope.body,
                token=token,
            )
            reply_text = self.response_extractor.extract_text(response)
            reply = RabbitMQReply(
                schema_version=REPLY_SCHEMA_VERSION,
                status="ok",
                conversation_id=envelope.conversation_id,
                message_id=envelope.message_id,
                from_gateway=self.worker_endpoint.display_name,
                to_gateway=envelope.from_gateway,
                return_session_key=envelope.return_session_key,
                created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                reply_text=reply_text,
            )
            self.broker.publish_reply_message(reply.to_payload())
            delivery.ack()
            self.logger.info(
                "worker adapter processed message=%s worker=%s",
                envelope.message_id,
                self.worker_endpoint.display_name,
            )
            return True
        except (EnvelopeError, ResponsesClientError, ResponseExtractorError, ConfigError, OSError) as exc:
            if _is_timeout_error(exc):
                delivery.ack()
                self.logger.warning(
                    "worker adapter timed out after dispatch to worker=%s message=%s; awaiting session reply",
                    self.worker_endpoint.display_name,
                    envelope.message_id if envelope is not None else "unknown",
                )
                return True
            reply_payload = self._build_error_reply(raw_payload=raw_payload, envelope=envelope, error=exc)
            try:
                self.broker.publish_reply_message(reply_payload)
            except RabbitMQError:
                delivery.nack(requeue=True)
                raise
            delivery.ack()
            self.logger.error(
                "worker adapter failed for worker=%s: %s",
                self.worker_endpoint.display_name,
                exc,
            )
            return True
        except Exception:
            delivery.nack(requeue=True)
            raise

    def _build_error_reply(
        self,
        *,
        raw_payload: dict[str, Any] | None,
        envelope: Envelope | None,
        error: Exception,
    ) -> dict[str, object]:
        if envelope is not None:
            conversation_id = envelope.conversation_id
            message_id = envelope.message_id
            to_gateway = envelope.from_gateway
            return_session_key = envelope.return_session_key
        else:
            conversation_id = str((raw_payload or {}).get("conversationId") or (raw_payload or {}).get("messageId") or "unknown-conversation")
            message_id = str((raw_payload or {}).get("messageId") or f"ERR-{int(time.time())}")
            to_gateway = str((raw_payload or {}).get("fromGateway") or self.config.endpoint_a.display_name)
            return_session_key = str(
                (raw_payload or {}).get("returnSessionKey")
                or self.config.endpoint_a.default_session_key
                or _default_human_session_key(self.config.endpoint_a.agent_id)
            )
        reply = RabbitMQReply(
            schema_version=REPLY_SCHEMA_VERSION,
            status="error",
            conversation_id=conversation_id,
            message_id=message_id,
            from_gateway=self.worker_endpoint.display_name,
            to_gateway=to_gateway,
            return_session_key=return_session_key,
            created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            error_text=str(error),
        )
        return reply.to_payload()

    def _resolve_endpoint_token(self, endpoint: EndpointConfig) -> str:
        token = os.environ.get(endpoint.token_env)
        if token:
            return token
        if endpoint.tunnel is None:
            return endpoint.resolve_token()
        try:
            return self.remote_token_resolver.fetch_token(endpoint.tunnel)
        except RemoteError as exc:
            raise ConfigError(
                f"failed to fetch token for {endpoint.display_name}: {exc}"
            ) from exc


class RabbitMQReplyConsumer:
    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        *,
        broker: RabbitMQBroker | None = None,
        responses_client: ResponsesClient | None = None,
        remote_token_resolver: RemoteTokenResolver | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.broker = broker or RabbitMQBroker(config)
        self.responses_client = responses_client or ResponsesClient()
        self.remote_token_resolver = remote_token_resolver or RemoteTokenResolver()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def run(self, *, once: bool) -> int:
        _ensure_rabbitmq_topology_if_available(self.broker, self.logger)
        processed = 0
        while self._running:
            handled = self.process_one()
            if handled:
                processed += 1
            if once:
                return processed
            if not handled:
                time.sleep(1.0)
        return processed

    def process_one(self) -> bool:
        binding = self.broker.relay_reply_binding()
        delivery = self.broker.consume_one(binding.queue)
        if delivery is None:
            return False

        try:
            payload = json.loads(delivery.body.decode("utf-8"))
            reply = RabbitMQReply.from_payload(payload)
            inject_content = self._build_inject_content(reply)
            token = self._resolve_endpoint_token(self.config.endpoint_a)
            self.responses_client.send_user_message(
                endpoint=self.config.endpoint_a,
                session_key=reply.return_session_key,
                content=inject_content,
                token=token,
            )
            delivery.ack()
            self.logger.info(
                "reply consumer injected message=%s status=%s",
                reply.message_id,
                reply.status,
            )
            return True
        except (ResponsesClientError, ConfigError, ValueError, OSError) as exc:
            self.logger.error("reply consumer failed: %s", exc)
            delivery.nack(requeue=True)
            return False
        except Exception:
            delivery.nack(requeue=True)
            raise

    def _build_inject_content(self, reply: RabbitMQReply) -> str:
        if reply.status == "ok":
            body = reply.reply_text or ""
            return (
                f"[Reply from {reply.from_gateway}]"
                f"[MESSAGE {reply.message_id}]"
                f"[CONVERSATION {reply.conversation_id}]\n"
                f"{body}"
            )
        return (
            f"[Reply from {reply.from_gateway}]"
            f"[MESSAGE {reply.message_id}]"
            f"[CONVERSATION {reply.conversation_id}]"
            f"[ERROR]\n"
            f"{reply.error_text or 'unknown error'}"
        )

    def _resolve_endpoint_token(self, endpoint: EndpointConfig) -> str:
        token = os.environ.get(endpoint.token_env)
        if token:
            return token
        if endpoint.tunnel is None:
            return endpoint.resolve_token()
        try:
            return self.remote_token_resolver.fetch_token(endpoint.tunnel)
        except RemoteError as exc:
            raise ConfigError(
                f"failed to fetch token for {endpoint.display_name}: {exc}"
            ) from exc


def _is_timeout_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    return "timed out" in text or "timeout" in text


def _ensure_rabbitmq_topology_if_available(broker: object, logger: logging.Logger) -> None:
    ensure_topology = getattr(broker, "ensure_topology", None)
    if not callable(ensure_topology):
        return
    ensure_topology()
    logger.info("rabbitmq topology ensured")
