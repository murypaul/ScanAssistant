"""Export-queue side panel, read-only.

Shows whatever `ExportQueue.pending_tasks()` currently holds. Since the
queue is drained with a bounded time budget per tick rather than all at
once, this can show a real backlog during a burst of captures, not just
a momentary flicker.
"""

from __future__ import annotations

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from scanassistant.core.queue import ExportTask
from scanassistant.i18n import t

_COLUMNS = ("name", "kind")


class ExportQueuePanel(QTableWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.verticalHeader().setVisible(False)
        self.setColumnCount(len(_COLUMNS))
        self.setHorizontalHeaderLabels([t(f"export_queue.column_{c}") for c in _COLUMNS])

    def refresh(self, tasks: list[ExportTask]) -> None:
        self.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            self.setItem(row, 0, QTableWidgetItem(task.name))
            self.setItem(row, 1, QTableWidgetItem(task.kind))
        self.resizeColumnsToContents()
