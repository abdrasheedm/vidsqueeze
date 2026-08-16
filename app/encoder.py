"""Build and run ffmpeg encode commands with live progress."""
import os
import subprocess
import threading
from dataclasses import dataclass

from .config import FFMPEG


@dataclass
class EncodeOptions:
    profile: str = "quality"          # "quality" (libx265) or "fast" (hevc_videotoolbox)
    crf: int = 32                     # quality profile
    preset: str = "slow"              # quality profile: medium | slow
    vt_quality: int = 55              # fast profile: -q:v 1..100 (higher = better)
    max_long_edge: int = 1920         # None/0 = keep original resolution
    audio_bitrate: str = "128k"


def build_command(src, dst, opts, pr):
    """pr is a ProbeResult for src."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", src, "-map_metadata", "0", "-map_chapters", "0"]

    if opts.max_long_edge:
        m = int(opts.max_long_edge)
        # Cap the long edge; works for landscape and portrait, never upscales.
        cmd += ["-vf",
                f"scale=w='min(iw,{m})':h='min(ih,{m})':"
                f"force_original_aspect_ratio=decrease:force_divisible_by=2"]

    ten_bit = pr.is_10bit or pr.is_hdr
    if opts.profile == "fast":
        cmd += ["-c:v", "hevc_videotoolbox", "-q:v", str(opts.vt_quality)]
        if ten_bit:
            cmd += ["-profile:v", "main10", "-pix_fmt", "p010le"]
        else:
            cmd += ["-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx265", "-preset", opts.preset, "-crf", str(opts.crf),
                "-x265-params", "log-level=error",
                "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]

    for flag, val in (("-color_primaries", pr.color_primaries),
                      ("-color_trc", pr.color_transfer),
                      ("-colorspace", pr.color_space)):
        if val and val != "unknown":
            cmd += [flag, val]

    cmd += ["-c:a", "aac", "-b:a", opts.audio_bitrate]
    cmd += ["-movflags", "+faststart", "-tag:v", "hvc1"]
    cmd += ["-progress", "pipe:1", dst]
    return cmd


class Encoder:
    """Runs one ffmpeg encode; exposes .progress (0..1) and .cancel()."""

    def __init__(self):
        self.progress = 0.0
        self.speed = ""
        self._proc = None
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def run(self, src, dst, opts, pr, log_path):
        cmd = build_command(src, dst, opts, pr)
        duration_us = max(pr.duration, 0.01) * 1_000_000
        with open(log_path, "a") as log:
            log.write("+ " + " ".join(cmd) + "\n")
            log.flush()
            with self._lock:
                if self._cancelled:
                    raise EncodeCancelled()
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=log, text=True)
            for line in self._proc.stdout:
                key, _, val = line.strip().partition("=")
                if key == "out_time_us" and val.lstrip("-").isdigit():
                    self.progress = min(int(val) / duration_us, 1.0)
                elif key == "speed":
                    self.speed = val
            rc = self._proc.wait()
        if self._cancelled:
            if os.path.exists(dst):
                os.remove(dst)
            raise EncodeCancelled()
        if rc != 0:
            if os.path.exists(dst):
                os.remove(dst)
            raise RuntimeError(f"ffmpeg exited with code {rc} (see {log_path})")
        self.progress = 1.0


class EncodeCancelled(Exception):
    pass
