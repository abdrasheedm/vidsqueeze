#!/bin/bash
# One-time setup: system tools + Python environment. macOS and Linux.
# Windows: use setup.ps1 instead.
set -e
cd "$(dirname "$0")"

install_tool() {
  local bin="$1" brew_pkg="$2" apt_pkg="$3" brew_flags="$4"
  command -v "$bin" >/dev/null && return 0
  if command -v brew >/dev/null; then
    # shellcheck disable=SC2086
    brew install $brew_flags "$brew_pkg"
  elif command -v apt-get >/dev/null; then
    sudo apt-get install -y "$apt_pkg"
  else
    echo "Could not install $bin automatically — install it and re-run." >&2
    return 1
  fi
}

if ! command -v brew >/dev/null && ! command -v apt-get >/dev/null; then
  echo "Homebrew (macOS) or apt (Linux) is required: https://brew.sh" >&2
  exit 1
fi

install_tool ffmpeg ffmpeg ffmpeg
install_tool exiftool exiftool libimage-exiftool-perl
install_tool adb android-platform-tools adb --cask || \
  echo "adb not installed — phone mode unavailable, folder mode still works."

[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip fastapi uvicorn

# ffmpeg lists every encoder it was built with, so the only honest test is to
# run one. app.hwaccel does exactly that.
echo
echo "Hardware encoder check:"
PYTHONPATH="$PWD" .venv/bin/python -c \
  "from app import hwaccel; print('  ' + hwaccel.summary()['detail'])"

echo
echo "Setup complete. Start the app with ./run.sh"
