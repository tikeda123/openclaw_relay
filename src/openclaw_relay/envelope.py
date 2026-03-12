from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

LEGACY_SCHEMA_VERSION = "relay-envelope/v1"
MESSAGE_SCHEMA_VERSION = "relay-message/v1"
DEFAULT_HUMAN_SESSION_KEY = "agent:main:main"
RELAY_SESSION_PREFIX = "relay"


class EnvelopeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Envelope:
    schema_version: str
    task_id: str
    turn_id: str
    from_gateway: str
    from_agent: str
    to_gateway: str
    to_agent: str
    intent: str
    body: str
    state_ref: str | None
    return_session_key: str
    approval_required: bool
    ttl_seconds: int
    idempotency_key: str
    created_at: datetime

    @property
    def conversation_id(self) -> str:
        return self.task_id

    @property
    def message_id(self) -> str:
        return self.turn_id

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "from": self.from_gateway,
            "to": self.to_gateway,
            "fromGateway": self.from_gateway,
            "toGateway": self.to_gateway,
            "body": self.body,
            "createdAt": self.created_at.isoformat(timespec="seconds"),
            "ttlSeconds": self.ttl_seconds,
            "idempotencyKey": self.idempotency_key,
        }
        if self.schema_version == MESSAGE_SCHEMA_VERSION:
            payload["conversationId"] = self.task_id
            payload["messageId"] = self.turn_id
        else:
            payload["taskId"] = self.task_id
            payload["turnId"] = self.turn_id
            payload["approvalRequired"] = self.approval_required
        if self.from_agent:
            payload["fromAgent"] = self.from_agent
        if self.to_agent:
            payload["toAgent"] = self.to_agent
        if self.intent:
            payload["intent"] = self.intent
        if self.state_ref:
            payload["stateRef"] = self.state_ref
        if self.return_session_key:
            payload["returnSessionKey"] = self.return_session_key
        if self.schema_version == LEGACY_SCHEMA_VERSION:
            payload["approvalRequired"] = self.approval_required
        elif self.approval_required:
            payload["approvalRequired"] = self.approval_required
        return payload

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        expected_schema_version: str,
        now: datetime | None = None,
        default_ttl_seconds: int = 300,
        default_from_gateway: str | None = None,
    ) -> "Envelope":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise EnvelopeError("invalid_encoding", f"{path.name}: not valid UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise EnvelopeError("invalid_json", f"{path.name}: invalid JSON ({exc.msg})") from exc

        if not isinstance(raw, dict):
            raise EnvelopeError("invalid_json", f"{path.name}: root JSON value must be an object")

        schema_version_value = raw.get("schemaVersion")
        if schema_version_value is None:
            schema_version = MESSAGE_SCHEMA_VERSION
        elif isinstance(schema_version_value, str) and schema_version_value.strip():
            schema_version = schema_version_value.strip()
        else:
            raise EnvelopeError("invalid_schema", "field 'schemaVersion' must be a non-empty string")
        if schema_version not in _accepted_schema_versions(expected_schema_version):
            raise EnvelopeError(
                "invalid_schema_version",
                (
                    f"{path.name}: schemaVersion {schema_version!r} "
                    f"does not match expected {expected_schema_version!r}"
                ),
            )
        envelope = _parse_envelope(
            raw,
            schema_version=schema_version,
            default_ttl_seconds=default_ttl_seconds,
            default_from_gateway=default_from_gateway,
            default_message_id=_default_message_id_for_path(path),
            default_created_at=_default_created_at_for_path(path),
        )

        reference_time = now or datetime.now(tz=envelope.created_at.tzinfo)
        if envelope.created_at + timedelta(seconds=envelope.ttl_seconds) <= reference_time:
            raise EnvelopeError("expired", f"{path.name}: envelope TTL has expired")

        return envelope

    @classmethod
    def from_payload(
        cls,
        raw: dict[str, Any],
        *,
        expected_schema_version: str,
        now: datetime | None = None,
        default_ttl_seconds: int = 300,
        default_from_gateway: str | None = None,
        default_message_id: str | None = None,
        default_created_at: datetime | None = None,
    ) -> "Envelope":
        if not isinstance(raw, dict):
            raise EnvelopeError("invalid_json", "payload root JSON value must be an object")

        schema_version_value = raw.get("schemaVersion")
        if schema_version_value is None:
            schema_version = MESSAGE_SCHEMA_VERSION
        elif isinstance(schema_version_value, str) and schema_version_value.strip():
            schema_version = schema_version_value.strip()
        else:
            raise EnvelopeError("invalid_schema", "field 'schemaVersion' must be a non-empty string")
        if schema_version not in _accepted_schema_versions(expected_schema_version):
            raise EnvelopeError(
                "invalid_schema_version",
                (
                    f"schemaVersion {schema_version!r} "
                    f"does not match expected {expected_schema_version!r}"
                ),
            )
        message_id = default_message_id or f"MSG-{hashlib.sha1(json.dumps(raw, sort_keys=True, ensure_ascii=True).encode('utf-8')).hexdigest()[:12]}"
        created_at = default_created_at or datetime.now(timezone.utc)
        envelope = _parse_envelope(
            raw,
            schema_version=schema_version,
            default_ttl_seconds=default_ttl_seconds,
            default_from_gateway=default_from_gateway,
            default_message_id=message_id,
            default_created_at=created_at,
        )
        reference_time = now or datetime.now(tz=envelope.created_at.tzinfo)
        if envelope.created_at + timedelta(seconds=envelope.ttl_seconds) <= reference_time:
            raise EnvelopeError("expired", "payload TTL has expired")
        return envelope


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError("invalid_schema", f"missing or invalid string field {key!r}")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a non-empty string")
    return value


def _optional_alias_str(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_str(data, key)
        if value is not None:
            return value
    return None


def _require_alias_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise EnvelopeError("invalid_schema", f"field {key!r} must be a non-empty string")
        return value.strip()
    joined = " / ".join(repr(key) for key in keys)
    raise EnvelopeError("invalid_schema", f"missing or invalid string field {joined}")


def _optional_str_with_default(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a non-empty string")
    return value


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a boolean")
    return value


def _optional_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a boolean")
    return value


def _require_positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or value <= 0:
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a positive integer")
    return value


def _optional_positive_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0:
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a positive integer")
    return value


def _require_datetime(data: dict[str, Any], key: str) -> datetime:
    raw = _require_str(data, key)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EnvelopeError("invalid_schema", f"field {key!r} must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise EnvelopeError("invalid_schema", f"field {key!r} must include timezone offset")
    return parsed


def _optional_datetime(data: dict[str, Any], key: str) -> datetime | None:
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise EnvelopeError("invalid_schema", f"field {key!r} must be ISO8601")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EnvelopeError("invalid_schema", f"field {key!r} must be ISO8601") from exc
    if parsed.tzinfo is None:
        raise EnvelopeError("invalid_schema", f"field {key!r} must include timezone offset")
    return parsed


def _accepted_schema_versions(expected_schema_version: str) -> set[str]:
    if expected_schema_version in {LEGACY_SCHEMA_VERSION, MESSAGE_SCHEMA_VERSION}:
        return {LEGACY_SCHEMA_VERSION, MESSAGE_SCHEMA_VERSION}
    return {expected_schema_version}


def build_relay_session_key(*, to_gateway: str, conversation_id: str) -> str:
    gateway_component = _normalize_session_component(to_gateway)
    conversation_component = _normalize_session_component(conversation_id)
    return f"{RELAY_SESSION_PREFIX}:{gateway_component}:{conversation_component}"


def resolve_return_session_key(
    requested_session_key: str | None,
    *,
    to_gateway: str,
    conversation_id: str,
) -> str:
    if requested_session_key is None:
        return DEFAULT_HUMAN_SESSION_KEY

    normalized = requested_session_key.strip()
    if not normalized:
        raise EnvelopeError("invalid_schema", "field 'returnSessionKey' must be a non-empty string")
    if normalized.lower() == "main":
        return DEFAULT_HUMAN_SESSION_KEY
    return normalized


def _parse_envelope(
    raw: dict[str, Any],
    *,
    schema_version: str,
    default_ttl_seconds: int,
    default_from_gateway: str | None,
    default_message_id: str,
    default_created_at: datetime,
) -> Envelope:
    if schema_version == MESSAGE_SCHEMA_VERSION:
        message_id = _optional_str(raw, "messageId") or default_message_id
        conversation_id = _optional_str(raw, "conversationId") or message_id
        from_gateway = _optional_alias_str(raw, "from", "fromGateway") or default_from_gateway
        if not isinstance(from_gateway, str) or not from_gateway.strip():
            raise EnvelopeError("invalid_schema", "missing or invalid string field 'from' or 'fromGateway'")
        to_gateway = _require_alias_str(raw, "to", "toGateway")
        return Envelope(
            schema_version=schema_version,
            task_id=conversation_id,
            turn_id=message_id,
            from_gateway=from_gateway,
            from_agent=_optional_str_with_default(raw, "fromAgent", "main"),
            to_gateway=to_gateway,
            to_agent=_optional_str_with_default(raw, "toAgent", "main"),
            intent=_optional_str_with_default(raw, "intent", "message"),
            body=_require_str(raw, "body"),
            state_ref=_optional_str(raw, "stateRef"),
            return_session_key=resolve_return_session_key(
                _optional_str(raw, "returnSessionKey"),
                to_gateway=to_gateway,
                conversation_id=conversation_id,
            ),
            approval_required=_optional_bool(raw, "approvalRequired", False),
            ttl_seconds=_optional_positive_int(raw, "ttlSeconds", default_ttl_seconds),
            idempotency_key=_optional_str(raw, "idempotencyKey") or message_id,
            created_at=_optional_datetime(raw, "createdAt") or default_created_at,
        )

    task_id = _require_str(raw, "taskId")
    to_gateway = _require_str(raw, "toGateway")
    return Envelope(
        schema_version=schema_version,
        task_id=task_id,
        turn_id=_require_str(raw, "turnId"),
        from_gateway=_require_str(raw, "fromGateway"),
        from_agent=_require_str(raw, "fromAgent"),
        to_gateway=to_gateway,
        to_agent=_require_str(raw, "toAgent"),
        intent=_require_str(raw, "intent"),
        body=_require_str(raw, "body"),
        state_ref=_optional_str(raw, "stateRef"),
        return_session_key=resolve_return_session_key(
            _require_str(raw, "returnSessionKey"),
            to_gateway=to_gateway,
            conversation_id=task_id,
        ),
        approval_required=_require_bool(raw, "approvalRequired"),
        ttl_seconds=_require_positive_int(raw, "ttlSeconds"),
        idempotency_key=_require_str(raw, "idempotencyKey"),
        created_at=_require_datetime(raw, "createdAt"),
    )


def _normalize_session_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not cleaned:
        return "default"
    return cleaned[:64]


def _default_message_id_for_path(path: Path) -> str:
    stat = path.stat()
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", path.stem).strip("-")
    if not stem:
        stem = "message"
    digest = hashlib.sha1(
        f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:10]
    return f"MSG-{stem}-{digest}"


def _default_created_at_for_path(path: Path) -> datetime:
    stat = path.stat()
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
