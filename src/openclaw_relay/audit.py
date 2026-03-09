from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from openclaw_relay.state import StateStore


@dataclass(frozen=True)
class AuditLogger:
    jsonl_path: Path
    mode: str
    store: StateStore

    def append(
        self,
        event_type: str,
        *,
        message_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.mode.lower() in {"off", "disabled", "none"}:
            return

        event_id = uuid4().hex
        event = {
            "eventId": event_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "eventType": event_type,
            "payload": _mask_secrets(payload or {}),
        }
        if message_id is not None:
            event["messageId"] = message_id

        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        self.store.record_audit_event(
            message_id=message_id,
            event_type=event_type,
            event_ref=event_id,
        )


def _mask_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(marker in lowered for marker in ("token", "secret", "password", "authorization")):
                masked[key] = "***REDACTED***"
            else:
                masked[key] = _mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value
