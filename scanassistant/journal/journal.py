"""JSONL business journal.

One object per line, daily file `LOGS/events_YYYY-MM-DD.jsonl`, UTF-8,
opened in append mode, flushed after every write. The journal is not
localized: it stores codes and raw values, never already-translated
strings — only their on-screen presentation goes through `t()`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

Level = Literal["info", "warn", "error"]
Result = Literal["ok", "error"]


class Journal:
    """Writes business events to `LOGS/events_YYYY-MM-DD.jsonl`."""

    def __init__(self, logs_dir: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self._logs_dir = Path(logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._now = now

    def log(
        self,
        event_type: str,
        action: str,
        *,
        level: Level = "info",
        image: str | None = None,
        details: dict[str, object] | None = None,
        result: Result = "ok",
    ) -> None:
        """Appends a line to today's journal."""
        ts = self._now()
        entry: dict[str, object] = {
            "ts": ts.isoformat(timespec="milliseconds"),
            "level": level,
            "type": event_type,
        }
        if image is not None:
            entry["image"] = image
        entry["action"] = action
        entry["details"] = details or {}
        entry["result"] = result

        line = json.dumps(entry, ensure_ascii=False)
        path = self._event_file_path(ts)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    def _event_file_path(self, ts: datetime) -> Path:
        return self._logs_dir / f"events_{ts:%Y-%m-%d}.jsonl"
