"""Update check/apply off the main thread.

`updater.check_for_update()`/`apply_update()` shell out to `git`/`pip`
(network I/O, several seconds on a slow link): calling them from the Qt
thread would violate the "never blocked > 100 ms" budget, same reasoning
as `PreviewWorker`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from scanassistant.updater import (
    UpdateApplyResult,
    UpdateCheckResult,
    apply_update,
    check_for_update,
    install_camera_dependencies,
)


class UpdateCheckWorker(QThread):
    finished_check = Signal(object)  # UpdateCheckResult

    def __init__(self, app_dir: Path) -> None:
        super().__init__()
        self._app_dir = app_dir

    def run(self) -> None:
        result: UpdateCheckResult = check_for_update(self._app_dir)
        self.finished_check.emit(result)


class UpdateApplyWorker(QThread):
    finished_apply = Signal(object)  # UpdateApplyResult

    def __init__(self, app_dir: Path, python_executable: str) -> None:
        super().__init__()
        self._app_dir = app_dir
        self._python_executable = python_executable

    def run(self) -> None:
        result: UpdateApplyResult = apply_update(self._app_dir, self._python_executable)
        self.finished_apply.emit(result)


class CameraDependencyInstallWorker(QThread):
    finished_install = Signal(object)  # UpdateApplyResult

    def __init__(self, app_dir: Path, python_executable: str) -> None:
        super().__init__()
        self._app_dir = app_dir
        self._python_executable = python_executable

    def run(self) -> None:
        result: UpdateApplyResult = install_camera_dependencies(
            self._app_dir, self._python_executable
        )
        self.finished_install.emit(result)
