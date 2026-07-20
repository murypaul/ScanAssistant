"""Session-history side panel: thumbnails of recently finalized images.

Lets the operator click back to a just-captured image to fix a mistake
(wrong rotation, bad crop) without hunting through the CSV viewer, which
refuses to reposition the cursor on a `done` row. Scoped to the campaign
currently open (`core.session.CaptureSession.session_history`, not
persisted to disk — lost if the app closes, but survives leaving and
re-entering capture mode) — this is a shortcut for catching a mistake made
earlier in this sitting, not a project-wide gallery.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from scanassistant.core.session import SessionHistoryEntry
from scanassistant.project.layout import CampaignPaths

_THUMBNAIL_SIZE = 96
_NAME_ROLE = Qt.ItemDataRole.UserRole


class HistoryPanel(QListWidget):
    image_activated = Signal(str)  # name of the image to reopen

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setFlow(QListWidget.Flow.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListWidget.Movement.Static)
        self.setIconSize(QSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # A single click reopens the image (small thumbnail gallery, not a
        # file browser) — `itemActivated` would require a double-click or a
        # keyboard selection that `NoSelection` mode doesn't support here.
        self.itemClicked.connect(self._on_item_clicked)
        self._thumbnail_loaded: set[str] = set()

    def clear_history(self) -> None:
        self.clear()
        self._thumbnail_loaded.clear()

    def refresh(
        self, entries: list[SessionHistoryEntry], paths: CampaignPaths, positive_suffix: str
    ) -> None:
        existing_names = {
            self.item(i).data(_NAME_ROLE)
            for i in range(self.count())  # type: ignore[union-attr]
        }
        for entry in entries:
            if entry.name in existing_names:
                continue
            item = QListWidgetItem(entry.name)
            item.setData(_NAME_ROLE, entry.name)
            item.setToolTip(entry.name)
            self.insertItem(0, item)  # most recently finalized on top

        for entry in entries:
            if entry.name in self._thumbnail_loaded:
                continue
            pixmap = self._load_thumbnail(entry.name, paths, positive_suffix)
            if pixmap is None:
                continue  # export not written yet — retried on the next refresh
            item = self._find_item(entry.name)
            if item is not None:
                item.setIcon(QIcon(pixmap))
            self._thumbnail_loaded.add(entry.name)

    def _find_item(self, name: str) -> QListWidgetItem | None:
        for i in range(self.count()):
            item = self.item(i)
            if item is not None and item.data(_NAME_ROLE) == name:
                return item
        return None

    def _load_thumbnail(
        self, name: str, paths: CampaignPaths, positive_suffix: str
    ) -> QPixmap | None:
        candidates = (
            paths.jpeg_positive_dir / f"{name}{positive_suffix}.jpg",
            paths.jpeg_master_dir / f"{name}.jpg",
        )
        for candidate in candidates:
            if not candidate.exists():
                continue
            pixmap = QPixmap(str(candidate))
            if pixmap.isNull():
                continue
            return pixmap.scaled(
                _THUMBNAIL_SIZE,
                _THUMBNAIL_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return None

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(_NAME_ROLE)
        if name:
            self.image_activated.emit(name)
