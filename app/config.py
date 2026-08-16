"""Shared paths and tool locations."""
import os
import shutil

DATA_DIR = os.path.expanduser("~/VideoCompressor")
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
LOG_DIR = os.path.join(DATA_DIR, "logs")
# Legacy dirs from v1; kept so old data stays reachable, no longer written to.
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
WORK_IN_DIR = os.path.join(DATA_DIR, "work", "in")
WORK_OUT_DIR = os.path.join(DATA_DIR, "work", "out")
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.json")
PIDFILE = os.path.join(DATA_DIR, "server.pid")

# Roots on the phone to scan (recursively) for videos.
PHONE_VIDEO_DIRS = ["/sdcard/DCIM", "/sdcard/Movies", "/sdcard/Pictures", "/sdcard/Download"]

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".3gp")

HOST = "127.0.0.1"
PORT = 8765


def _find_tool(name):
    path = shutil.which(name) or shutil.which(name, path="/opt/homebrew/bin:/usr/local/bin")
    return path or name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")
EXIFTOOL = _find_tool("exiftool")
ADB = _find_tool("adb")


def ensure_data_dirs():
    for d in (LIBRARY_DIR, THUMB_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
