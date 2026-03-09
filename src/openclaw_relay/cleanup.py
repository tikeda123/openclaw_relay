from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openclaw_relay.config import RelayConfig
from openclaw_relay.state import StateStore


@dataclass(frozen=True)
class CleanupResult:
    counts: dict[str, int]
    deleted_paths: tuple[Path, ...]
    cutoff: datetime


@dataclass(frozen=True)
class ArtifactCleaner:
    store: StateStore
    relay: RelayConfig

    def cleanup(self, *, older_than_days: int, dry_run: bool = False) -> CleanupResult:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        counts = {
            "archive": 0,
            "deadletter": 0,
            "done_processing": 0,
            "done_replies": 0,
        }
        deleted_paths: list[Path] = []

        for path in self._iter_old_files(self.relay.archive_dir, cutoff=cutoff):
            if self._delete(path, dry_run=dry_run):
                counts["archive"] += 1
                deleted_paths.append(path)

        for path in self._iter_old_files(self.relay.deadletter_dir, cutoff=cutoff):
            if self._delete(path, dry_run=dry_run):
                counts["deadletter"] += 1
                deleted_paths.append(path)

        for row in self.store.list_messages_by_status(("DONE",)):
            if not _timestamp_older_than(row["updated_at"], cutoff):
                continue
            for kind, raw_path in (
                ("done_processing", row["processing_path"]),
                ("done_replies", row["reply_path"]),
            ):
                if not raw_path:
                    continue
                path = Path(raw_path)
                if self._delete(path, dry_run=dry_run):
                    counts[kind] += 1
                    deleted_paths.append(path)

        return CleanupResult(
            counts=counts,
            deleted_paths=tuple(deleted_paths),
            cutoff=cutoff,
        )

    def _iter_old_files(self, directory: Path, *, cutoff: datetime) -> list[Path]:
        if not directory.exists():
            return []
        cutoff_ts = cutoff.timestamp()
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file() and path.stat().st_mtime <= cutoff_ts
        )

    def _delete(self, path: Path, *, dry_run: bool) -> bool:
        if not path.exists():
            return False
        if dry_run:
            return True
        path.unlink()
        return True


def _timestamp_older_than(raw: str | None, cutoff: datetime) -> bool:
    if not raw:
        return False
    parsed = _parse_timestamp(raw)
    return parsed <= cutoff


def _parse_timestamp(raw: str) -> datetime:
    normalized = raw.strip()
    for candidate in (
        normalized,
        normalized.replace(" ", "T"),
    ):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ValueError(f"unsupported timestamp format: {raw!r}")
