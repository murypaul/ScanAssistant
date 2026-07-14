#!/usr/bin/env bash
# Crée/active un venv puis lance ScanAssistant (12_CONTRAINTES_TECHNIQUES.md §6).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
fi

exec .venv/bin/python -m scanassistant "$@"
