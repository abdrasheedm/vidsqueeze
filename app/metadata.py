"""Copy all metadata tags from the original into the compressed file."""
import os
import subprocess

from .config import EXIFTOOL


def copy_all_tags(src, dst):
    """exiftool pass: copy every writable tag (dates, GPS, maker data) from src.

    Rotation/matrix are excluded: ffmpeg bakes the rotation into the pixels,
    so re-applying the original rotation tag would display the video sideways.
    """
    cmd = [EXIFTOOL, "-overwrite_original", "-api", "LargeFileSupport=1",
           "-TagsFromFile", src, "-all:all>all:all",
           "--QuickTime:Rotation", "--QuickTime:MatrixStructure",
           dst]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"exiftool failed: {out.stderr.strip() or out.stdout.strip()}")


def restore_file_times(src, dst):
    st = os.stat(src)
    os.utime(dst, (st.st_atime, st.st_mtime))
