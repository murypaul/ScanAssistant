"""Business exceptions carrying a normative error code.

These exceptions only carry the code and raw details: translating them
into a displayable message (`t()`) is the presentation layer's job (GUI),
never the core's (`project` never imports PySide6 or
`scanassistant.i18n`).
"""

from __future__ import annotations


class ScanAssistantError(Exception):
    """Common base: normative code + structured details."""

    code: str = ""

    def __init__(self, details: dict[str, object] | None = None) -> None:
        self.details: dict[str, object] = details or {}
        super().__init__(self.code, self.details)

    def __str__(self) -> str:
        return f"{self.code} {self.details}"


class InvalidCampaignError(ScanAssistantError):
    """E-10: invalid `campaign.json`."""

    code = "E-10"

    def __init__(self, field: str, detail: str) -> None:
        super().__init__({"field": field, "detail": detail})


class InvalidCsvError(ScanAssistantError):
    """E-11: invalid CSV on import."""

    code = "E-11"

    def __init__(self, problems: list[str]) -> None:
        super().__init__({"problems": problems})


class ProjectAlreadyOpenError(ScanAssistantError):
    """E-14: project already open elsewhere (lock held by a live PID)."""

    code = "E-14"

    def __init__(self, host: str, pid: int) -> None:
        super().__init__({"host": host, "pid": pid})
