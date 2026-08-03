"""Technical log.

Standard `logging` → `debug.log`, rotating at 5 × 2 MB, INFO level by
default, DEBUG via `--debug`. Before a campaign is open, the file lives
in the user log directory (`platformdirs`); once a campaign opens,
`setup_logging` is called again with `<campaign>/LOGS` to switch output
(no campaign exists yet at startup).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

import platformdirs

from scanassistant.config import APP_NAME

LOGGER_NAME = "scanassistant"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_logger = logging.getLogger(LOGGER_NAME)
_handler: logging.Handler | None = None


def default_log_dir() -> Path:
    """Location of technical logs outside of any open campaign."""
    return Path(platformdirs.user_log_dir(APP_NAME))


def setup_logging(log_dir: Path | None = None, debug: bool = False) -> Path:
    """(Re)configures the technical log to `<log_dir>/debug.log`."""
    global _handler
    log_dir = log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "debug.log"

    if _handler is not None:
        _logger.removeHandler(_handler)
        _handler.close()

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.DEBUG if debug else logging.INFO)
    _logger.propagate = False
    _handler = handler
    return log_path


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def install_excepthook() -> None:
    """Routes any otherwise-uncaught exception — main thread, a background
    thread, or one escaping a PySide6 slot (Qt calls `sys.excepthook` for
    those too) — into `debug.log` instead of stderr.

    Without this, an exception here has nowhere else to go: a desktop
    launcher commonly starts the app with stdout/stderr closed or piped to
    `/dev/null`, so the default `sys.excepthook` output is simply lost, and
    the operator is left looking at a screen that stopped updating with no
    error, no log line, and no way to tell what happened or that a restart
    would fix it.
    """

    def _log_main_thread(exc_type: type[BaseException], exc_value: BaseException, exc_tb) -> None:
        get_logger().critical("unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    def _log_thread(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread is not None else "?"
        get_logger().critical(
            "unhandled exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_main_thread
    threading.excepthook = _log_thread
