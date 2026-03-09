from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any


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

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        expected_schema_version: str,
        now: datetime | None = None,
    ) -> "Envelope":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise EnvelopeError("invalid_encoding", f"{path.name}: not valid UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise EnvelopeError("invalid_json", f"{path.name}: invalid JSON ({exc.msg})") from exc

        if not isinstance(raw, dict):
            raise EnvelopeError("invalid_json", f"{path.name}: root JSON value must be an object")

        envelope = cls(
            schema_version=_require_str(raw, "schemaVersion"),
            task_id=_require_str(raw, "taskId"),
            turn_id=_require_str(raw, "turnId"),
            from_gateway=_require_str(raw, "fromGateway"),
            from_agent=_require_str(raw, "fromAgent"),
            to_gateway=_require_str(raw, "toGateway"),
            to_agent=_require_str(raw, "toAgent"),
            intent=_require_str(raw, "intent"),
            body=_require_str(raw, "body"),
            state_ref=_optional_str(raw, "stateRef"),
            return_session_key=_require_str(raw, "returnSessionKey"),
            approval_required=_require_bool(raw, "approvalRequired"),
            ttl_seconds=_require_positive_int(raw, "ttlSeconds"),
            idempotency_key=_require_str(raw, "idempotencyKey"),
            created_at=_require_datetime(raw, "createdAt"),
        )

        if envelope.schema_version != expected_schema_version:
            raise EnvelopeError(
                "invalid_schema_version",
                (
                    f"{path.name}: schemaVersion {envelope.schema_version!r} "
                    f"does not match expected {expected_schema_version!r}"
                ),
            )

        reference_time = now or datetime.now(tz=envelope.created_at.tzinfo)
        if envelope.created_at + timedelta(seconds=envelope.ttl_seconds) <= reference_time:
            raise EnvelopeError("expired", f"{path.name}: envelope TTL has expired")

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


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise EnvelopeError("invalid_schema", f"field {key!r} must be a boolean")
    return value


def _require_positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
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
