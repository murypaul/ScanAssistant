"""Statistics and completeness screen.

Standalone window (like the shortcuts help, `main_window.py`) rather than
another screen in the home/project/capture stack: "Statistics" is an
auxiliary view usable alongside the rest of the app (also accessible
outside of capture), not one of its main modes.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scanassistant.core.completeness import (
    CompletenessGap,
    check_completeness,
    regenerate_selection,
)
from scanassistant.core.recovery import read_journal_entries
from scanassistant.core.session import CaptureSession
from scanassistant.gui.widgets.pin_checkbox import make_pin_checkbox
from scanassistant.i18n import t
from scanassistant.project.inventory import STATUS_COLUMN

_NAME_COLUMN = 1
_MISSING_COLUMN = 2
# Regeneration submits to a background executor (`_build_offline_session`)
# and returns immediately — this polls until the submitted backlog has
# actually drained, so the list/status can reflect real progress instead
# of refreshing once, instantly, against work that hasn't run yet.
_REGEN_POLL_INTERVAL_MS = 500


class StatisticsScreen(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("statistics.title"))
        self.resize(640, 420)

        self._session: CaptureSession | None = None
        self._gaps: list[CompletenessGap] = []
        self._regenerating_names: set[str] = set()
        self._regen_total = 0
        self._regen_timer = QTimer(self)
        self._regen_timer.setInterval(_REGEN_POLL_INTERVAL_MS)
        self._regen_timer.timeout.connect(self._poll_regeneration)

        self.explanation_label = QLabel(t("statistics.explanation"))
        self.explanation_label.setProperty("role", "secondary")
        self.explanation_label.setWordWrap(True)

        self.total_label = QLabel()
        self.done_label = QLabel()
        self.remaining_label = QLabel()
        self.rejected_label = QLabel()
        self.errors_label = QLabel()
        counters_row = QHBoxLayout()
        for label in (
            self.total_label,
            self.done_label,
            self.remaining_label,
            self.rejected_label,
            self.errors_label,
        ):
            counters_row.addWidget(label)
        counters_row.addStretch(1)

        self.check_button = QPushButton(t("statistics.completeness_check"))
        self.check_button.clicked.connect(self._on_check_completeness)
        self.regenerate_button = QPushButton(t("statistics.regenerate_selection"))
        self.regenerate_button.clicked.connect(self._on_regenerate_selection)
        self.regenerate_button.setEnabled(False)
        actions_row = QHBoxLayout()
        actions_row.addWidget(self.check_button)
        actions_row.addWidget(self.regenerate_button)
        actions_row.addStretch(1)

        self.gaps_table = QTableWidget()
        self.gaps_table.setColumnCount(3)
        self.gaps_table.setHorizontalHeaderLabels(
            ["", t("statistics.column_name"), t("statistics.column_missing")]
        )
        self.gaps_table.verticalHeader().setVisible(False)
        self.gaps_table.horizontalHeader().setSectionResizeMode(
            _MISSING_COLUMN, QHeaderView.ResizeMode.Stretch
        )

        self.status_label = QLabel()
        self.status_label.setProperty("role", "secondary")
        self.status_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(make_pin_checkbox(self))
        layout.addWidget(self.explanation_label)
        layout.addLayout(counters_row)
        layout.addLayout(actions_row)
        layout.addWidget(self.gaps_table, 1)
        layout.addWidget(self.status_label)

    def load(self, session: CaptureSession) -> None:
        """Reloads counters for a campaign (safe to call again on every open)."""
        self._regen_timer.stop()
        self._regenerating_names = set()
        self._session = session
        self._gaps = []
        self.gaps_table.setRowCount(0)
        self.check_button.setEnabled(True)
        self.regenerate_button.setEnabled(False)
        self.status_label.setText("")
        self._refresh_counters()

    def _refresh_counters(self) -> None:
        session = self._session
        if session is None:
            return
        rows = session.inventory.rows
        total = len(rows)
        done = sum(1 for row in rows if row.get(STATUS_COLUMN) == "done")
        remaining = total - done
        rejected = sum(
            1
            for entry in read_journal_entries(session.paths, session.fs)
            if entry.get("type") == "REJECT" and entry.get("action") == "rejected"
        )
        errors = len(session.state.error_images)

        self.total_label.setText(t("statistics.total", count=total))
        self.done_label.setText(t("statistics.done", count=done))
        self.remaining_label.setText(t("statistics.remaining", count=remaining))
        self.rejected_label.setText(t("statistics.rejected", count=rejected))
        self.errors_label.setText(t("statistics.errors", count=errors))

    def _on_check_completeness(self) -> None:
        session = self._session
        if session is None:
            self.status_label.setText(t("statistics.unavailable"))
            return
        self._refresh_counters()
        self._gaps = check_completeness(session)
        self._populate_gaps()

    def _populate_gaps(self) -> None:
        self._populate_gaps_table()
        self.regenerate_button.setEnabled(any(not gap.raw_missing for gap in self._gaps))
        self.status_label.setText("" if self._gaps else t("statistics.no_gaps"))

    def _populate_gaps_table(self) -> None:
        """Fills the table from `self._gaps` only — never touches
        `status_label`/`regenerate_button`, which `_poll_regeneration` is
        driving to a different message while a regeneration is running."""
        self.gaps_table.setRowCount(len(self._gaps))
        for row, gap in enumerate(self._gaps):
            checkbox = QCheckBox()
            checkbox.setChecked(not gap.raw_missing)  # nothing to regenerate without a RAW
            checkbox.setEnabled(not gap.raw_missing)
            self.gaps_table.setCellWidget(row, 0, checkbox)
            self.gaps_table.setItem(row, _NAME_COLUMN, QTableWidgetItem(gap.name))
            missing_text = (
                t("statistics.raw_missing") if gap.raw_missing else ", ".join(gap.missing_kinds)
            )
            self.gaps_table.setItem(row, _MISSING_COLUMN, QTableWidgetItem(missing_text))

    def _on_regenerate_selection(self) -> None:
        session = self._session
        if session is None:
            return
        selected = [
            gap.name
            for row, gap in enumerate(self._gaps)
            if not gap.raw_missing and self._is_row_checked(row)
        ]
        if not selected:
            return
        # `regenerate_selection` only *submits* to the background executor
        # (`_build_offline_session`) and returns immediately — the actual
        # renders can take anywhere from a few seconds to over a minute
        # each. Without polling, re-checking completeness right away would
        # just show the exact same gaps, with nothing on screen to tell the
        # operator anything had even started.
        regenerate_selection(session, selected)
        self._regenerating_names = set(selected)
        self._regen_total = len(selected)
        self.check_button.setEnabled(False)
        self.regenerate_button.setEnabled(False)
        self.status_label.setText(
            t("statistics.regenerating", remaining=self._regen_total, total=self._regen_total)
        )
        self._regen_timer.start()

    def _poll_regeneration(self) -> None:
        session = self._session
        if session is None:
            self._regen_timer.stop()
            return
        session.collect_export_progress()
        self._gaps = check_completeness(session)
        self._populate_gaps_table()
        self._refresh_counters()
        remaining = sum(1 for gap in self._gaps if gap.name in self._regenerating_names)
        queue_drained = len(session.export_queue) == 0
        if remaining == 0 or queue_drained:
            self._regen_timer.stop()
            self._regenerating_names = set()
            self.check_button.setEnabled(True)
            self.regenerate_button.setEnabled(any(not gap.raw_missing for gap in self._gaps))
            if remaining == 0:
                self.status_label.setText(t("statistics.regeneration_complete"))
            else:
                self.status_label.setText(
                    t(
                        "statistics.regeneration_stopped_with_gaps_left",
                        remaining=remaining,
                        total=self._regen_total,
                    )
                )
            return
        self.status_label.setText(
            t("statistics.regenerating", remaining=remaining, total=self._regen_total)
        )

    def _is_row_checked(self, row: int) -> bool:
        checkbox = self.gaps_table.cellWidget(row, 0)
        return isinstance(checkbox, QCheckBox) and checkbox.isChecked()
