"""In-place update check/apply.

The one narrow, documented exception to the offline rule (CLAUDE.md règle
absolue 3, I-102, DECISIONS.md): every network call here is a plain
`git fetch`/`git pull` against whatever remote and branch the checkout
already tracks — no hardcoded URL, no GitHub API, no telemetry. This
module never decides *when* to run; the caller (GUI) only invokes it on an
explicit operator action or an opt-in startup check.

Reuses the same mechanism `install.sh`/`install.ps1` already trust
(`git pull --ff-only`) rather than a separate download/package system.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 30
_PIP_TIMEOUT_S = 300


@dataclass(frozen=True)
class UpdateCheckResult:
    available: bool
    local_commit: str | None = None
    remote_commit: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class UpdateApplyResult:
    success: bool
    output: str
    error: str | None = None


def is_git_checkout(app_dir: Path) -> bool:
    return (app_dir / ".git").exists()


def _run_git(
    app_dir: Path, *args: str, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=app_dir, capture_output=True, text=True, timeout=timeout
    )


def check_for_update(app_dir: Path) -> UpdateCheckResult:
    """Fetches the tracked remote, then compares HEAD to `@{upstream}`.

    The single network call in the check path (`git fetch`). Never raises:
    any failure (no git checkout, no network, no upstream configured)
    comes back as `error` for the caller to display, never as an exception.
    """
    if not is_git_checkout(app_dir):
        return UpdateCheckResult(available=False, error="Not a git installation.")
    try:
        fetch = _run_git(app_dir, "fetch")
        if fetch.returncode != 0:
            return UpdateCheckResult(available=False, error=fetch.stderr.strip())
        local = _run_git(app_dir, "rev-parse", "--short", "HEAD")
        remote = _run_git(app_dir, "rev-parse", "--short", "@{upstream}")
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateCheckResult(available=False, error=str(exc))
    if local.returncode != 0 or remote.returncode != 0:
        return UpdateCheckResult(available=False, error=(local.stderr or remote.stderr).strip())

    local_commit = local.stdout.strip()
    remote_commit = remote.stdout.strip()
    return UpdateCheckResult(
        available=local_commit != remote_commit,
        local_commit=local_commit,
        remote_commit=remote_commit,
    )


def list_local_changes(app_dir: Path) -> list[str]:
    """Tracked files with uncommitted changes (staged or not) — what
    `apply_update(discard_local_changes=True)` would permanently discard.
    Untracked files are excluded (`git status --porcelain`'s `??` lines):
    `git pull --ff-only` never refuses because of those alone, so
    discarding them would offer no benefit while adding real risk. Empty
    on any git failure (no checkout, no repo) — a caller can't usefully
    distinguish "nothing to discard" from "can't tell" here, since either
    means offering to discard would be meaningless.
    """
    try:
        status = _run_git(app_dir, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    if status.returncode != 0:
        return []
    return [
        line[3:].strip()
        for line in status.stdout.splitlines()
        if line and not line.startswith("??")
    ]


def apply_update(
    app_dir: Path, python_executable: str, *, discard_local_changes: bool = False
) -> UpdateApplyResult:
    """`git pull --ff-only` then reinstalls dependencies in place.

    Runs in `app_dir` (the current installation, never a different
    location) with `python_executable` (the interpreter already running
    the app, so the reinstall lands in the same virtual environment).
    Stops at the first failure — a failed `pull` never triggers `pip
    install`, and `--ff-only` refuses to touch the working tree rather
    than force anything.

    `discard_local_changes`: for the operator-confirmed retry after a
    first attempt was blocked by uncommitted local changes
    (`list_local_changes`) — `git reset --hard HEAD` first, discarding
    them but never touching which commit is checked out (a genuinely
    diverged/unpushed-commits checkout, a different problem, still stops
    the following `--ff-only` pull exactly as before).
    """
    if discard_local_changes:
        try:
            reset = _run_git(app_dir, "reset", "--hard", "HEAD")
        except (OSError, subprocess.SubprocessError) as exc:
            return UpdateApplyResult(success=False, output="", error=str(exc))
        if reset.returncode != 0:
            return UpdateApplyResult(success=False, output=reset.stdout, error=reset.stderr.strip())

    try:
        pull = _run_git(app_dir, "pull", "--ff-only")
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateApplyResult(success=False, output="", error=str(exc))
    if pull.returncode != 0:
        return UpdateApplyResult(success=False, output=pull.stdout, error=pull.stderr.strip())

    try:
        install = subprocess.run(
            [python_executable, "-m", "pip", "install", "-e", "."],
            cwd=app_dir,
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateApplyResult(success=False, output=pull.stdout, error=str(exc))
    if install.returncode != 0:
        return UpdateApplyResult(
            success=False, output=pull.stdout + install.stdout, error=install.stderr.strip()
        )

    return UpdateApplyResult(success=True, output=pull.stdout + install.stdout)


def install_camera_dependencies(app_dir: Path, python_executable: str) -> UpdateApplyResult:
    """`pip install -e ".[camera]"` — no `git pull`.

    Run when the operator turns tethered capture on
    (`preferences.py:_on_camera_enabled_changed`) and `gphoto2` isn't
    importable yet in the running venv: the extra is opt-in (pyproject.toml
    `camera`, needs the system `libgphoto2` besides the Python binding) so
    a user who never plugs in a camera never pays for it, but flipping the
    switch must not just leave them to hit `ModuleNotFoundError` the next
    time they open Capture.
    """
    try:
        install = subprocess.run(
            [python_executable, "-m", "pip", "install", "-e", ".[camera]"],
            cwd=app_dir,
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return UpdateApplyResult(success=False, output="", error=str(exc))
    if install.returncode != 0:
        return UpdateApplyResult(success=False, output=install.stdout, error=install.stderr.strip())

    return UpdateApplyResult(success=True, output=install.stdout)
