@echo off
REM Cree/active un venv puis lance ScanAssistant (12_CONTRAINTES_TECHNIQUES.md par 6).
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv
    .venv\Scripts\pip install --upgrade pip
    .venv\Scripts\pip install -e .
)

.venv\Scripts\python -m scanassistant %*
