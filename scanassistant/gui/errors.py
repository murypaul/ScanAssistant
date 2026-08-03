"""Formats normative error/warning codes into their catalogued message.

`09_ERREURS_ET_ROBUSTESSE.md` §3 defines an exact message per code, but
`core`/`project` never import `scanassistant.i18n` (they only carry a
code + raw `details`, per their own module docstrings) — translating them
into displayable text is this module's job, the only place in the GUI
layer that should ever touch `error.*` catalog keys directly.
"""

from __future__ import annotations

from scanassistant.camera.errors import (
    CODE_CAPTURE_TIMEOUT,
    CODE_LIVE_VIEW_FAILED,
    CODE_NOT_DETECTED,
    CODE_SEEN_AS_STORAGE,
    CODE_TRIGGER_FAILED,
    CODE_USB_BUSY,
)
from scanassistant.i18n import t
from scanassistant.project.errors import (
    InvalidCampaignError,
    InvalidCsvError,
    MissingInventoryError,
    ProjectAlreadyOpenError,
    ScanAssistantError,
)

_WARNING_KEYS = {
    "E-01": "error.E-01_warning",
    "E-03": "error.E-03",
    "E-04": "error.E-04",
    "E-08": "error.E-08",
    "E-09": "error.E-09",
    "E-15": "error.E-15",
    # Camera (remote trigger + live view): always a warning, never critical —
    # a tethering hiccup must never suspend the folder-watching pipeline.
    CODE_NOT_DETECTED: "error.E-17",
    CODE_USB_BUSY: "error.E-18",
    CODE_SEEN_AS_STORAGE: "error.E-19",
    CODE_TRIGGER_FAILED: "error.E-20",
    CODE_LIVE_VIEW_FAILED: "error.E-21",
    CODE_CAPTURE_TIMEOUT: "error.E-22",
}

_CRITICAL_KEYS = {
    "E-01": "error.E-01_critical",
    "E-02": "error.E-02",
    "E-07": "error.E-07",
    "E-12": "error.E-12",
    "E-13": "error.E-13",
}


def format_warning(code: str, details: dict[str, object]) -> str:
    """A yellow-banner warning event's catalogued message (`core.events.Warning`).

    A-01 (exiftool unavailable) already carries its own pre-formatted text
    in `details["message"]` — used as-is rather than looked up by code.
    """
    if "message" in details:
        return str(details["message"])
    key = _WARNING_KEYS.get(code)
    if key is None:
        return t("error.generic")
    return t(key, **details)


def format_critical(code: str, details: dict[str, object]) -> str:
    """A red-banner critical event's catalogued message (`core.events.CriticalError`)."""
    key = _CRITICAL_KEYS.get(code)
    if key is None:
        return t("error.generic")
    return t(key, **details)


def format_business_error(exc: ScanAssistantError) -> str:
    """A `project.errors.ScanAssistantError` subclass's catalogued message."""
    if isinstance(exc, InvalidCampaignError):
        return t("error.E-10", **exc.details)
    if isinstance(exc, InvalidCsvError):
        problems = exc.details.get("problems", [])
        assert isinstance(problems, list)
        lines = [t("error.E-11", count=len(problems))]
        lines += [f"• {p}" for p in problems]
        return "\n".join(lines)
    if isinstance(exc, ProjectAlreadyOpenError):
        return t("error.E-14", **exc.details)
    if isinstance(exc, MissingInventoryError):
        text = t("error.E-16")
        if exc.details.get("has_backup"):
            text += t("error.E-16_backup_hint")
        return text
    return t("error.generic")
