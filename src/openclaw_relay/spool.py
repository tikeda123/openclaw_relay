from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil


@dataclass(frozen=True)
class SpoolWriter:
    processing_dir: Path
    archive_dir: Path
    deadletter_dir: Path

    def local_dirs(self) -> tuple[Path, ...]:
        return (self.processing_dir, self.archive_dir, self.deadletter_dir)

    def copy_to_processing(self, source: Path) -> Path:
        return self._copy(source, self.processing_dir, source.name)

    def copy_to_archive(self, source: Path, *, reason: str) -> Path:
        return self._copy(source, self.archive_dir, _decorate_name(source, reason))

    def copy_to_deadletter(self, source: Path, *, reason: str) -> Path:
        return self._copy(source, self.deadletter_dir, _decorate_name(source, reason))

    def _copy(self, source: Path, destination_dir: Path, name: str) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(destination_dir, name)
        shutil.copy2(source, destination)
        return destination


def _decorate_name(source: Path, reason: str) -> str:
    safe_reason = reason.replace(" ", "_").lower()
    return f"{source.stem}__{safe_reason}{source.suffix}"


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        numbered = directory / f"{stem}_{index}{suffix}"
        if not numbered.exists():
            return numbered
        index += 1
