"""Capture core exceptions.

Same conventions as `project/errors.py`: normative code + raw details,
never an already-translated message.
"""

from __future__ import annotations

from scanassistant.project.errors import ScanAssistantError


class IllegalTransitionError(Exception):
    """Programming error: forbidden state-machine transition.

    Deliberately not a `ScanAssistantError`: this isn't an operational
    situation but a call-site bug.
    """

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal transition: {current} -> {target}")


class IntegrityCheckFailedError(ScanAssistantError):
    """E-04: ingestion verification failed (size or SHA-256)."""

    code = "E-04"

    def __init__(self, name: str) -> None:
        super().__init__({"name": name})


class InventoryExhaustedError(ScanAssistantError):
    """E-12: CSV exhausted."""

    code = "E-12"


class WatchedFolderInaccessibleError(ScanAssistantError):
    """E-07: watched folder inaccessible during capture."""

    code = "E-07"

    def __init__(self, reason: str) -> None:
        super().__init__({"reason": reason})


class NameConflictError(ScanAssistantError, ValueError):
    """Name conflict: needs operator arbitration (replace/rename) before
    the operation (ingestion, or `CaptureSession.rename_current`) can
    proceed. Also a `ValueError` — a caller that only distinguishes
    operator-input problems in general (an invalid name, a name already
    in use with no arbitration offered) still catches this without
    needing to know about it specifically."""

    code = "conflict"

    def __init__(self, name: str, existing_path: str) -> None:
        super().__init__({"name": name, "existing_path": existing_path})
