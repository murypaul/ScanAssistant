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
