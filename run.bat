@echo off
REM Cree/active un venv puis lance ScanAssistant (12_CONTRAINTES_TECHNIQUES.md par 6).
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv
    .venv\Scripts\pip install --upgrade pip
)
REM Always run, not just on first creation: a git pull (manual or via the
REM in-app updater, I-102) can change dependencies without recreating .venv.
REM No-op and fast when nothing changed.
.venv\Scripts\pip install -e .

.venv\Scripts\python -m scanassistant %*
