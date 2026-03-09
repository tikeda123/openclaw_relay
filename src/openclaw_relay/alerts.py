from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import threading


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_ts(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(alert: dict[str, object]) -> str:
    fingerprint = alert.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.strip():
        return fingerprint
    encoded = json.dumps(alert, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass
class AlertStore:
    path: Path
    max_history: int = 100

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, object]] = {}
        self._recent: list[dict[str, object]] = []
        self._last_received_at: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        active = raw.get("active")
        recent = raw.get("recent")
        last_received_at = raw.get("lastReceivedAt")
        if isinstance(active, dict):
            self._active = {
                str(key): value
                for key, value in active.items()
                if isinstance(value, dict)
            }
        if isinstance(recent, list):
            self._recent = [item for item in recent if isinstance(item, dict)][: self.max_history]
        if isinstance(last_received_at, str) and last_received_at.strip():
            self._last_received_at = last_received_at

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lastReceivedAt": self._last_received_at,
            "active": self._active,
            "recent": self._recent[: self.max_history],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def record_webhook(self, payload: dict[str, object]) -> dict[str, int]:
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            raise ValueError("alertmanager webhook payload is missing alerts")

        now_iso = _utc_now_iso()
        received = 0
        with self._lock:
            self._last_received_at = now_iso
            for raw_alert in alerts:
                if not isinstance(raw_alert, dict):
                    continue
                normalized = self._normalize_alert(raw_alert, received_at=now_iso)
                fingerprint = str(normalized["fingerprint"])
                if normalized["status"] == "resolved":
                    self._active.pop(fingerprint, None)
                else:
                    self._active[fingerprint] = normalized
                self._recent.insert(0, normalized)
                received += 1
            self._recent = self._recent[: self.max_history]
            self._persist()
            active_count = len(self._active)
        return {"received": received, "active": active_count}

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            active = list(self._active.values())
            recent = list(self._recent[: self.max_history])
            last_received_at = self._last_received_at
        active.sort(
            key=lambda item: (
                0 if item.get("severity") == "critical" else 1,
                str(item.get("startsAt") or ""),
                str(item.get("alertname") or ""),
            ),
            reverse=False,
        )
        summary: dict[str, int] = {"total": len(active)}
        for alert in active:
            severity = str(alert.get("severity") or "unknown")
            summary[severity] = summary.get(severity, 0) + 1
        return {
            "active": active,
            "recent": recent,
            "summary": summary,
            "lastReceivedAt": last_received_at,
        }

    def _normalize_alert(
        self,
        alert: dict[str, object],
        *,
        received_at: str,
    ) -> dict[str, object]:
        labels = alert.get("labels")
        annotations = alert.get("annotations")
        labels_dict = labels if isinstance(labels, dict) else {}
        annotations_dict = annotations if isinstance(annotations, dict) else {}
        return {
            "fingerprint": _fingerprint(alert),
            "status": str(alert.get("status") or "firing"),
            "alertname": str(labels_dict.get("alertname") or "unknown"),
            "severity": str(labels_dict.get("severity") or "unknown"),
            "worker": str(labels_dict.get("worker") or ""),
            "endpoint": str(labels_dict.get("endpoint") or ""),
            "target": str(labels_dict.get("target") or ""),
            "summary": str(annotations_dict.get("summary") or ""),
            "description": str(annotations_dict.get("description") or ""),
            "startsAt": _normalize_ts(alert.get("startsAt")),
            "endsAt": _normalize_ts(alert.get("endsAt")),
            "updatedAt": received_at,
            "generatorURL": str(alert.get("generatorURL") or ""),
        }
