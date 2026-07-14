"""Read-only CSV table.

Columns: row #, name, status, `source_file`, then the remaining CSV
columns, in that order. Reused by the wizard (preview of the first 20
rows) and by the project screen's CSV tab (full set, cursor row
highlighted).
"""

from __future__ import annotations

from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QWidget

from scanassistant.i18n import t
from scanassistant.project.inventory import SOURCE_FILE_COLUMN, STATUS_COLUMN


def ordered_columns(fieldnames: list[str], name_column: str) -> list[str]:
    """Ordre normatif : nom, statut, source_file, puis le reste (06 §6)."""
    rest = [f for f in fieldnames if f not in (name_column, STATUS_COLUMN, SOURCE_FILE_COLUMN)]
    return [name_column, STATUS_COLUMN, SOURCE_FILE_COLUMN, *rest]


class CsvTableWidget(QTableWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)

    def populate(
        self,
        *,
        fieldnames: list[str],
        name_column: str,
        rows: list[dict[str, str]],
        cursor: int | None = None,
        limit: int | None = None,
    ) -> None:
        columns = ordered_columns(fieldnames, name_column)
        headers = [t("csv.column_line")] + [
            t("csv.column_name") if c == name_column else _column_header(c) for c in columns
        ]
        display_rows = rows if limit is None else rows[:limit]

        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.setRowCount(len(display_rows))

        for row_index, row in enumerate(display_rows):
            self.setItem(row_index, 0, QTableWidgetItem(str(row_index + 1)))
            for col_index, column in enumerate(columns, start=1):
                self.setItem(row_index, col_index, QTableWidgetItem(row.get(column, "")))

        self.resizeColumnsToContents()
        if cursor is not None and 0 <= cursor < len(display_rows):
            self.selectRow(cursor)  # cursor row highlighted


def _column_header(column: str) -> str:
    known = {
        STATUS_COLUMN: t("csv.column_status"),
        SOURCE_FILE_COLUMN: t("csv.column_source_file"),
    }
    return known.get(column, column)
