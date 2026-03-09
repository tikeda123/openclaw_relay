from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Watcher:
    watch_dir: Path

    def list_pending_files(self) -> list[Path]:
        if not self.watch_dir.exists():
            return []
        if not self.watch_dir.is_dir():
            raise NotADirectoryError(f"{self.watch_dir} is not a directory")

        return sorted(
            path
            for path in self.watch_dir.iterdir()
            if path.is_file() and path.suffix == ".json" and not path.name.endswith(".tmp")
        )
