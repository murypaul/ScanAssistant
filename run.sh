#!/usr/bin/env bash
# Crée/active un venv puis lance ScanAssistant.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
fi
# Always run, not just on first creation: a `git pull` (manual or via the
# in-app updater, I-102) can change dependencies without recreating .venv.
# No-op and fast when nothing changed.
.venv/bin/pip install -e .

exec .venv/bin/python -m scanassistant "$@"
