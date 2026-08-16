"""ffprobe wrapper: extract the stream facts the pipeline needs."""
import json
import subprocess
from dataclasses import dataclass

from .config import FFPROBE

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


@dataclass
class ProbeResult:
    width: int
    height: int
    duration: float
    fps: float
    video_codec: str
    pix_fmt: str
    bit_depth: int
    color_primaries: str
    color_transfer: str
    color_space: str
    bit_rate: int
    rotation: int

    @property
    def is_hdr(self):
        return self.color_transfer in HDR_TRANSFERS

    @property
    def is_10bit(self):
        return self.bit_depth >= 10

    @property
    def display_size(self):
        # Size as shown after rotation is applied
        if self.rotation % 180 != 0:
            return (self.height, self.width)
        return (self.width, self.height)


def _parse_fps(rate):
    try:
        num, _, den = rate.partition("/")
        den = float(den or 1)
        return round(float(num) / den, 3) if den else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path):
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {out.stderr.strip()}")
    data = json.loads(out.stdout)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"No video stream in {path}")

    pix_fmt = video.get("pix_fmt", "") or ""
    bit_depth = 10 if ("10" in pix_fmt or pix_fmt.startswith("p010")) else 8

    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"]) % 360

    fmt = data.get("format", {})
    duration = float(video.get("duration") or fmt.get("duration") or 0)
    bit_rate = int(fmt.get("bit_rate") or 0)

    return ProbeResult(
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        duration=duration,
        fps=_parse_fps(video.get("avg_frame_rate", "0/1")),
        video_codec=video.get("codec_name", ""),
        pix_fmt=pix_fmt,
        bit_depth=bit_depth,
        color_primaries=video.get("color_primaries", "") or "",
        color_transfer=video.get("color_transfer", "") or "",
        color_space=video.get("color_space", "") or "",
        bit_rate=bit_rate,
        rotation=rotation,
    )
