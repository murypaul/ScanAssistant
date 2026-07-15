"""Home screen.

Three areas: New campaign, Open campaign, recent projects (up to 10, with
path and last-opened date; entry grayed out if inaccessible).
`config.general.recent_projects` only stores paths: the displayed date is
derived from each campaign's `state.json` `mtime` rather than persisted
separately.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scanassistant.i18n import t
from scanassistant.project.layout import CampaignPaths

_PATH_ROLE = Qt.ItemDataRole.UserRole


class HomeScreen(QWidget):
    new_campaign_requested = Signal()
    open_campaign_requested = Signal()
    recent_campaign_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel(t("home.title"))
        title.setStyleSheet("font-size: 22pt; font-weight: bold;")

        self._new_button = QPushButton(t("home.new_campaign"))
        self._new_button.clicked.connect(self.new_campaign_requested)
        self._open_button = QPushButton(t("home.open_campaign"))
        self._open_button.clicked.connect(self.open_campaign_requested)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self._new_button)
        buttons_row.addWidget(self._open_button)
        buttons_row.addStretch(1)

        recent_label = QLabel(t("home.recent_projects"))
        recent_label.setProperty("role", "secondary")

        self._recent_list = QListWidget()
        self._recent_list.itemActivated.connect(self._on_recent_activated)

        # Opt-in startup update check only (CLAUDE.md règle absolue 3,
        # I-102) — discreet, never a popup, hidden unless there's actually
        # something to report.
        self._update_banner = QLabel()
        self._update_banner.setProperty("role", "secondary")
        self._update_banner.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(buttons_row)
        layout.addWidget(self._update_banner)
        layout.addSpacing(24)
        layout.addWidget(recent_label)
        layout.addWidget(self._recent_list, stretch=1)

        self.set_recent_projects([])

    def show_update_available(self, message: str) -> None:
        self._update_banner.setText(message)
        self._update_banner.setVisible(True)

    def set_recent_projects(self, paths: list[str]) -> None:
        self._recent_list.clear()
        if not paths:
            item = QListWidgetItem(t("home.recent_empty"))
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(item)
            return

        for path_str in paths:
            root = Path(path_str)
            accessible = _campaign_accessible(root)
            item = QListWidgetItem(_format_recent_entry(root, accessible))
            item.setData(_PATH_ROLE, path_str)
            if not accessible:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._recent_list.addItem(item)

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        path_str = item.data(_PATH_ROLE)
        if path_str:
            self.recent_campaign_chosen.emit(path_str)


def _campaign_accessible(root: Path) -> bool:
    return CampaignPaths(root).campaign_json.exists()


def _format_recent_entry(root: Path, accessible: bool) -> str:
    if not accessible:
        return t("home.recent_unavailable", path=str(root))
    try:
        mtime = CampaignPaths(root).state_json.stat().st_mtime
        date_text = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        date_text = t("home.recent_unknown_date")
    return t("home.recent_entry", path=str(root), date=date_text)
