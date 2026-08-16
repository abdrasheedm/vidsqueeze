"""Shared paths, platform detection and tool locations."""
import glob
import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# Name for "this computer" in user-facing strings, so the UI doesn't say "Mac"
# on a Windows box.
COMPUTER = "PC" if IS_WINDOWS else "Mac" if IS_MACOS else "computer"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything the app keeps — originals, compressed copies, logs, thumbnails —
# lives here. Defaults to a folder inside the checkout so a batch stays next to
# the app; set VIDSQUEEZE_DATA_DIR to put it elsewhere (an external drive, or
# the ~/VideoCompressor location used by earlier versions).
DATA_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("VIDSQUEEZE_DATA_DIR") or os.path.join(APP_DIR, "VideoCompressor")))
LIBRARY_DIR = os.path.join(DATA_DIR, "library")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
LOG_DIR = os.path.join(DATA_DIR, "logs")
# Legacy dirs from v1; kept so old data stays reachable, no longer written to.
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
WORK_IN_DIR = os.path.join(DATA_DIR, "work", "in")
WORK_OUT_DIR = os.path.join(DATA_DIR, "work", "out")
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.json")
PIDFILE = os.path.join(DATA_DIR, "server.pid")
HWCACHE_PATH = os.path.join(DATA_DIR, "hwaccel.json")

# Roots on the phone to scan (recursively) for videos.
PHONE_VIDEO_DIRS = ["/sdcard/DCIM", "/sdcard/Movies", "/sdcard/Pictures", "/sdcard/Download"]

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".avi", ".3gp")

HOST = "127.0.0.1"
PORT = 8765

# index.html is read from disk on every request, so a long-running server can
# serve a newer page than the code it is running. The page checks this number
# and warns instead of silently misbehaving. Bump it whenever the API changes.
API_VERSION = 2

# Stop ffmpeg/adb/exiftool from flashing a console window on Windows.
if IS_WINDOWS:
    NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    NO_WINDOW = {}


def _windows_hint_dirs():
    """Where the Windows installers in setup.ps1 actually drop their binaries."""
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(local, "Microsoft", "WinGet", "Links"),
        os.path.join(local, "Programs", "ExifTool"),
        os.path.join(home, "scoop", "shims"),
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "chocolatey", "bin"),
        os.path.join(local, "Android", "Sdk", "platform-tools"),
        r"C:\ffmpeg\bin",
    ]
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if pf:
            dirs += [os.path.join(pf, "ffmpeg", "bin"), os.path.join(pf, "ExifTool")]
    # winget "portable" packages unpack into per-package folders rather than Links.
    if local:
        dirs += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                              "*", "**", "bin"), recursive=False))
        dirs += sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*")))
    return dirs


def _hint_dirs():
    if IS_WINDOWS:
        return _windows_hint_dirs()
    if IS_MACOS:
        return ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    return ["/usr/local/bin", "/usr/bin", "/snap/bin"]


# Portable binaries can also be dropped next to the app, in ./tools/.
_TOOLS_DIR = os.path.join(APP_DIR, "tools")


def _find_tool(name):
    """Locate an external tool: env override, then PATH, then platform hints.

    shutil.which() handles the .exe/.bat lookup via PATHEXT on Windows, so the
    same bare name works everywhere. Falls back to the bare name so the caller
    still produces a sensible "not installed" error.
    """
    override = os.environ.get(f"VIDSQUEEZE_{name.upper()}")
    if override and os.path.isfile(override):
        return override

    found = shutil.which(name)
    if found:
        return found

    search = [_TOOLS_DIR, os.path.join(_TOOLS_DIR, "bin")] + _hint_dirs()
    found = shutil.which(name, path=os.pathsep.join(d for d in search if d))
    return found or name


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")
EXIFTOOL = _find_tool("exiftool")
ADB = _find_tool("adb")


def missing_tools():
    """Names of required tools that could not be located."""
    return [n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE),
                           ("exiftool", EXIFTOOL))
            if not os.path.isfile(p) and shutil.which(p) is None]


def ensure_data_dirs():
    for d in (LIBRARY_DIR, THUMB_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
