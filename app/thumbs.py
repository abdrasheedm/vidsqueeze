"""Thumbnail generation, both for phone files (pre-pull) and local files."""
import hashlib
import os
import subprocess

from .config import ADB, FFMPEG, NO_WINDOW, THUMB_DIR, ensure_data_dirs

# How much of a remote file to read when extracting a poster frame. MP4s written
# by the camera put the moov atom at the front, so the first few MB decode fine.
REMOTE_HEAD_MB = 12
THUMB_WIDTH = 320


def cache_key(source_path, mtime, size):
    raw = f"{source_path}|{mtime}|{size}".encode()
    return hashlib.sha1(raw).hexdigest()


def cached_thumb(key):
    p = os.path.join(THUMB_DIR, f"{key}.jpg")
    return p if os.path.exists(p) else None


def _extract_frame(input_arg, out_path, stdin_data=None, seek=None):
    """Run ffmpeg to write a single JPEG. Returns True on success."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if seek:
        cmd += ["-ss", str(seek)]
    cmd += ["-i", input_arg, "-frames:v", "1",
            "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "5", out_path]
    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True, timeout=90,
                           **NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def from_local(video_path, out_path, duration=None):
    """Poster frame from a local video: prefer ~10% in, fall back to the first frame."""
    ensure_data_dirs()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    seek = round(duration * 0.1, 2) if duration and duration > 3 else None
    if seek and _extract_frame(video_path, out_path, seek=seek):
        return out_path
    return out_path if _extract_frame(video_path, out_path) else None


def from_phone(remote_path, mtime, size):
    """Pull the head of a remote file and extract a frame from it. Cached."""
    ensure_data_dirs()
    key = cache_key(remote_path, mtime, size)
    out_path = os.path.join(THUMB_DIR, f"{key}.jpg")
    if os.path.exists(out_path):
        return out_path

    # exec-out gives raw binary on stdout (no line-ending translation).
    head_mb = min(REMOTE_HEAD_MB, max(1, size // (1024 * 1024) or 1))
    cmd = [ADB, "exec-out",
           f"dd if='{remote_path}' bs=1M count={head_mb} 2>/dev/null"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, **NO_WINDOW)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None

    # Feed the partial file to ffmpeg via stdin; truncated input is expected.
    if _extract_frame("pipe:0", out_path, stdin_data=r.stdout):
        return out_path
    return None
