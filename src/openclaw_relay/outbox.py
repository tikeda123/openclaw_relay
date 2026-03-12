from __future__ import annotations

from datetime import datetime
import json
from uuid import uuid4


def build_message_payload(
    *,
    from_gateway: str,
    to_gateway: str,
    body: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
    from_agent: str | None = None,
    to_agent: str | None = None,
    intent: str | None = None,
    return_session_key: str | None = None,
    ttl_seconds: int = 1800,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    normalized_message_id = message_id or f"MSG-{uuid4()}"
    normalized_conversation_id = conversation_id or normalized_message_id
    payload: dict[str, object] = {
        "schemaVersion": "relay-message/v1",
        "conversationId": normalized_conversation_id,
        "messageId": normalized_message_id,
        "from": from_gateway,
        "to": to_gateway,
        "fromGateway": from_gateway,
        "toGateway": to_gateway,
        "body": body,
        "ttlSeconds": ttl_seconds,
        "idempotencyKey": idempotency_key or normalized_message_id,
        "createdAt": (created_at or datetime.now().astimezone()).isoformat(timespec="seconds"),
    }
    if from_agent:
        payload["fromAgent"] = from_agent
    if to_agent:
        payload["toAgent"] = to_agent
    if intent:
        payload["intent"] = intent
    if return_session_key:
        payload["returnSessionKey"] = return_session_key
    return payload
