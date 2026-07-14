#!/usr/bin/env bash
# ScanAssistant installer (Linux).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.sh | bash
#   ./install.sh [target-directory]   # default: ~/ScanAssistant
set -euo pipefail

REPO_URL="https://github.com/murypaul/ScanAssistant.git"
ARCHIVE_URL="https://github.com/murypaul/ScanAssistant/archive/refs/heads/master.zip"
TARGET_DIR="${1:-$HOME/ScanAssistant}"

echo "== ScanAssistant installer =="

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 was not found. Install Python 3.11+ first:" >&2
    echo "  Debian/Ubuntu/Mint: sudo apt install python3 python3-venv" >&2
    exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')"
if [ "$PY_OK" != "1" ]; then
    echo "Error: Python 3.11+ is required (found $PY_VERSION)." >&2
    exit 1
fi

if [ -d "$TARGET_DIR/.git" ]; then
    echo "Existing installation found in $TARGET_DIR — updating..."
    git -C "$TARGET_DIR" pull --ff-only
elif command -v git >/dev/null 2>&1; then
    echo "Cloning ScanAssistant into $TARGET_DIR..."
    git clone --depth 1 "$REPO_URL" "$TARGET_DIR"
else
    echo "git not found — downloading a source archive instead..."
    if ! command -v curl >/dev/null 2>&1; then
        echo "Error: neither git nor curl is available. Install one of them first." >&2
        exit 1
    fi
    TMP_ZIP="$(mktemp -t scanassistant-XXXXXX.zip)"
    curl -fsSL "$ARCHIVE_URL" -o "$TMP_ZIP"
    mkdir -p "$TARGET_DIR"
    TMP_EXTRACT="$(mktemp -d)"
    unzip -q "$TMP_ZIP" -d "$TMP_EXTRACT"
    cp -a "$TMP_EXTRACT"/ScanAssistant-*/. "$TARGET_DIR"/
    rm -rf "$TMP_ZIP" "$TMP_EXTRACT"
fi

if ! command -v exiftool >/dev/null 2>&1; then
    echo "Note: exiftool was not found — metadata will be skipped with a warning" >&2
    echo "      until installed. Debian/Ubuntu/Mint: sudo apt install libimage-exiftool-perl" >&2
fi

echo "Setting up and launching ScanAssistant..."
exec bash "$TARGET_DIR/run.sh"
