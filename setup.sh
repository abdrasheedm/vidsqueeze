#!/bin/bash
# One-time setup: system tools + Python environment.
set -e
cd "$(dirname "$0")"

command -v brew >/dev/null || { echo "Homebrew is required: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v exiftool >/dev/null || brew install exiftool
command -v adb >/dev/null || brew install --cask android-platform-tools

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip fastapi uvicorn

echo "Setup complete. Start the app with ./run.sh"
