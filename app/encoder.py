"""Build and run ffmpeg encode commands with live progress.

The "fast" profile uses whatever hardware HEVC encoder this machine really has
(see hwaccel.py). Their quality knobs are not interchangeable: VideoToolbox
takes -q:v on a 1..100 scale where higher is better, while NVENC/QSV/AMF take a
CRF-like 0..51 value where *lower* is better. EncodeOptions therefore carries
one normalised 1..100 "higher is better" number and each encoder maps it.
"""
import os
import subprocess
import threading
from dataclasses import dataclass

from . import hwaccel
from .config import FFMPEG, NO_WINDOW

# Quality scale conversion. NVENC/QSV/AMF share ffmpeg's CRF-like 0..51 range,
# so one mapping serves all three: quality 30 -> 39, 55 -> 29, 80 -> 19.
_CQ_SLOPE = 0.40
_CQ_MIN, _CQ_MAX = 12, 45


def quality_to_cq(q):
    """Map the 1..100 'higher is better' scale onto a 0..51 CRF-like value."""
    return int(round(min(_CQ_MAX, max(_CQ_MIN, 51.0 - _CQ_SLOPE * float(q)))))


@dataclass
class EncodeOptions:
    profile: str = "quality"          # "quality" (libx265) or "fast" (hardware)
    crf: int = 32                     # quality profile
    preset: str = "slow"              # quality profile: medium | slow
    hw_quality: int = 55              # fast profile: 1..100, higher = better
    max_long_edge: int = 1920         # None/0 = keep original resolution
    audio_bitrate: str = "128k"
    hw_encoder: str = ""              # "" = auto-detect
    # Pacing for long runs: rest `rest_seconds` after every `batch_size` files.
    # 0 on either disables it.
    batch_size: int = 0
    rest_seconds: int = 0
    # Accepted for backwards compatibility with the VideoToolbox-only version.
    vt_quality: int = None

    def __post_init__(self):
        if self.vt_quality is not None:
            self.hw_quality = self.vt_quality
        self.vt_quality = self.hw_quality


def _hw_video_args(encoder, q, ten_bit, feats):
    """Per-encoder quality/pixel-format flags for the fast profile."""
    if encoder == "hevc_videotoolbox":
        # Native 1..100 scale, higher is better — used as-is.
        args = ["-c:v", encoder, "-q:v", str(int(q))]
        args += (["-profile:v", "main10", "-pix_fmt", "p010le"] if ten_bit
                 else ["-pix_fmt", "yuv420p"])
        return args

    cq = quality_to_cq(q)

    if encoder == "hevc_nvenc":
        args = ["-c:v", "hevc_nvenc",
                "-preset", "p6" if feats.get("tune_hq") else "p5"]
        if feats.get("tune_hq"):
            args += ["-tune", "hq"]
        # -b:v 0 is required: without it NVENC's VBR mode clamps to a default
        # bitrate and the -cq target is silently ignored.
        args += ["-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        if feats.get("bframes"):
            args += ["-bf", "3"]
        if feats.get("spatial_aq"):
            args += ["-spatial-aq", "1", "-aq-strength", "8"]
        if feats.get("temporal_aq"):
            args += ["-temporal-aq", "1"]
        if feats.get("lookahead"):
            args += ["-rc-lookahead", "32"]
        if feats.get("multipass"):
            args += ["-multipass", "fullres"]
        args += (["-profile:v", "main10", "-pix_fmt", "p010le"] if ten_bit
                 else ["-profile:v", "main", "-pix_fmt", "yuv420p"])
        return args

    if encoder == "hevc_qsv":
        args = ["-c:v", "hevc_qsv", "-preset", "slower",
                "-global_quality", str(cq)]
        if feats.get("bframes"):
            args += ["-bf", "3"]
        if feats.get("lookahead"):
            args += ["-look_ahead", "1"]
        args += (["-profile:v", "main10", "-pix_fmt", "p010le"] if ten_bit
                 else ["-profile:v", "main", "-pix_fmt", "nv12"])
        return args

    if encoder == "hevc_amf":
        args = ["-c:v", "hevc_amf", "-quality", "quality",
                "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq)]
        if feats.get("bframes"):
            args += ["-bf", "3"]
        args += (["-profile:v", "main10", "-pix_fmt", "p010le"] if ten_bit
                 else ["-profile:v", "main", "-pix_fmt", "nv12"])
        return args

    if encoder == "hevc_vaapi":
        args = ["-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(cq)]
        args += ["-profile:v", "main10"] if ten_bit else ["-profile:v", "main"]
        return args

    # Unknown encoder name (forced via env): trust it, pass the CRF-like value.
    return ["-c:v", encoder, "-global_quality", str(cq)]


def _software_fast_args(q, ten_bit):
    """Fallback when no hardware encoder works, so 'fast' never hard-fails."""
    return ["-c:v", "libx265", "-preset", "veryfast",
            "-crf", str(quality_to_cq(q)),
            "-x265-params", "log-level=error",
            "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p"]


def resolve_hw(opts):
    """Which encoder the fast profile will use, and what it can do."""
    if opts.hw_encoder:
        info = hwaccel.detect()
        if opts.hw_encoder == info.get("encoder"):
            return opts.hw_encoder, info["ten_bit"], info["features"]
        return opts.hw_encoder, True, {}
    info = hwaccel.detect()
    return info["encoder"], info["ten_bit"], info["features"]


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
        encoder, hw_10bit, feats = resolve_hw(opts)
        if not encoder:
            cmd += _software_fast_args(opts.hw_quality, ten_bit)
        else:
            # An 8-bit-only chip would wash out HDR footage, so drop back to
            # software rather than silently losing the extra bit depth.
            if ten_bit and not hw_10bit:
                cmd += _software_fast_args(opts.hw_quality, ten_bit)
            else:
                cmd += _hw_video_args(encoder, opts.hw_quality, ten_bit, feats)
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
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write("+ " + " ".join(cmd) + "\n")
            log.flush()
            with self._lock:
                if self._cancelled:
                    raise EncodeCancelled()
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=log, text=True,
                    encoding="utf-8", errors="replace", **NO_WINDOW)
            for line in self._proc.stdout:
                key, _, val = line.strip().partition("=")
                if key == "out_time_us" and val.lstrip("-").isdigit():
                    self.progress = min(int(val) / duration_us, 1.0)
                elif key == "speed":
                    self.speed = val
            rc = self._proc.wait()
        if self._cancelled:
            _unlink(dst)
            raise EncodeCancelled()
        if rc != 0:
            _unlink(dst)
            raise RuntimeError(f"ffmpeg exited with code {rc} (see {log_path})")
        self.progress = 1.0


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


class EncodeCancelled(Exception):
    pass
