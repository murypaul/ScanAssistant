"""Read-only table of today's journal."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from scanassistant.i18n import t

_COLUMNS = ("ts", "level", "type", "action", "image", "details", "result")


class LogTableWidget(QTableWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        self.setColumnCount(len(_COLUMNS))
        self.setHorizontalHeaderLabels([t(f"log.column_{c}") for c in _COLUMNS])

    def load_today(self, logs_dir: Path, *, type_filter: str = "", level_filter: str = "") -> None:
        path = logs_dir / f"events_{date.today():%Y-%m-%d}.jsonl"
        entries = _read_entries(path)
        if type_filter:
            entries = [e for e in entries if e.get("type") == type_filter]
        if level_filter:
            entries = [e for e in entries if e.get("level") == level_filter]
        self._populate(entries)

    def _populate(self, entries: list[dict[str, object]]) -> None:
        self.setRowCount(len(entries))
        for row_index, entry in enumerate(entries):
            for col_index, column in enumerate(_COLUMNS):
                value = entry.get(column, "")
                text = json.dumps(value) if isinstance(value, dict) else str(value)
                self.setItem(row_index, col_index, QTableWidgetItem(text))
        self.resizeColumnsToContents()


def _read_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    entries: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        entries.append(json.loads(line))
    return entries
