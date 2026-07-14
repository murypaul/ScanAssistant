"""GUI entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from scanassistant.app_context import AppContext
from scanassistant.gui.main_window import MainWindow
from scanassistant.gui.theme import apply_theme


def run_gui(context: AppContext | None = None) -> int:
    """Startup: global config → dark theme → home screen."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    assert isinstance(app, QApplication)
    resolved_context = context or AppContext.bootstrap()
    apply_theme(app, resolved_context.config.ui.brightness)

    window = MainWindow(resolved_context)
    window.show()

    return app.exec()
