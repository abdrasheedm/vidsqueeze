"""Pick the HEVC hardware encoder this machine can actually use.

`ffmpeg -encoders` lists what the binary was *built* with, not what the machine
can run. A stock Windows build advertises hevc_nvenc, hevc_qsv, hevc_amf and
hevc_vaapi on a box that has none of those GPUs, and even a real NVIDIA card
fails when the ffmpeg build wants a newer NVENC API than the installed driver
provides. So every candidate is verified by running a real encode before it is
offered, and the answer is cached — the probes cost a second or two, once.

Set VIDSQUEEZE_HW_ENCODER to force a specific encoder, or to "none" to disable
hardware encoding entirely.
"""
import json
import os
import re
import subprocess
import sys
import time

from .config import FFMPEG, HWCACHE_PATH, NO_WINDOW, ensure_data_dirs

# Preference order per platform. First one that survives the probe wins.
_CANDIDATES = {
    "darwin": ["hevc_videotoolbox"],
    "win32": ["hevc_nvenc", "hevc_qsv", "hevc_amf"],
}
_LINUX_CANDIDATES = ["hevc_nvenc", "hevc_qsv", "hevc_vaapi"]

LABELS = {
    "hevc_videotoolbox": "Apple VideoToolbox",
    "hevc_nvenc": "NVIDIA NVENC",
    "hevc_qsv": "Intel Quick Sync",
    "hevc_amf": "AMD AMF",
    "hevc_vaapi": "VA-API",
}

# Extra encoder options worth having but not supported by every chip generation
# (B-frames in HEVC need Turing or newer, for instance). Probed, not assumed.
_FEATURE_PROBES = {
    "hevc_nvenc": {
        "bframes": ["-bf", "3"],
        "spatial_aq": ["-spatial-aq", "1"],
        "temporal_aq": ["-temporal-aq", "1"],
        "multipass": ["-multipass", "fullres"],
        "tune_hq": ["-preset", "p6", "-tune", "hq"],
        "lookahead": ["-rc-lookahead", "32"],
    },
    "hevc_qsv": {
        "bframes": ["-bf", "3"],
        "lookahead": ["-look_ahead", "1"],
    },
    "hevc_amf": {
        "bframes": ["-bf", "3"],
    },
}

_PROBE_TIMEOUT = 60
# Bump whenever the probe set changes, so old caches are re-detected instead of
# silently missing newly probed features.
_SCHEMA = 1
_cache = None


def _candidates():
    return _CANDIDATES.get(sys.platform, _LINUX_CANDIDATES)


def _ffmpeg_version():
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-version"],
                             capture_output=True, text=True, timeout=30,
                             errors="replace", **NO_WINDOW)
        return out.stdout.splitlines()[0].strip() if out.stdout else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def built_with():
    """Encoder names this ffmpeg binary knows about (build flags, not hardware)."""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30,
                             errors="replace", **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"^\s*[VAS][\w.]*\s+(\S+)", out.stdout, re.M))


def _probe(encoder, extra, ten_bit=False):
    """Run a real 5-frame encode. Returns (ok, first line of stderr)."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-frames:v", "5"]
    if encoder == "hevc_vaapi":
        # VAAPI needs an explicit device and frames uploaded to the GPU.
        dev = os.environ.get("VIDSQUEEZE_VAAPI_DEVICE", "/dev/dri/renderD128")
        cmd[1:1] = ["-init_hw_device", f"vaapi=va:{dev}", "-filter_hw_device", "va"]
        cmd += ["-vf", f"format={'p010' if ten_bit else 'nv12'},hwupload"]
    elif ten_bit:
        cmd += ["-pix_fmt", "p010le", "-profile:v", "main10"]
    cmd += ["-c:v", encoder, *extra, "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=_PROBE_TIMEOUT, errors="replace", **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]
    if out.returncode == 0:
        return True, ""
    err = (out.stderr or "").strip().splitlines()
    return False, (err[0][:200] if err else f"exit {out.returncode}")


def detect(force=False):
    """Find the best usable hardware encoder. Cached in the data dir."""
    global _cache
    if _cache is not None and not force:
        return _cache

    version = _ffmpeg_version()
    if not force:
        cached = _read_cache()
        # Re-probe whenever the ffmpeg binary or its version changes, since that
        # is exactly what flips NVENC between working and not.
        if cached and cached.get("schema") == _SCHEMA \
                and cached.get("ffmpeg") == FFMPEG \
                and cached.get("ffmpeg_version") == version:
            _cache = cached
            return _cache

    result = {
        "schema": _SCHEMA,
        "ffmpeg": FFMPEG,
        "ffmpeg_version": version,
        "encoder": None,
        "label": None,
        "ten_bit": False,
        "features": {},
        "built_with": [],
        "rejected": {},
        "detected_at": int(time.time()),
    }

    forced = (os.environ.get("VIDSQUEEZE_HW_ENCODER") or "").strip()
    if forced.lower() in ("none", "off", "0"):
        result["rejected"]["*"] = "disabled by VIDSQUEEZE_HW_ENCODER"
        return _store(result)

    available = built_with()
    result["built_with"] = sorted(n for n in available if n.startswith("hevc_"))
    candidates = [forced] if forced else _candidates()

    for enc in candidates:
        if enc not in available:
            result["rejected"][enc] = "not in this ffmpeg build"
            continue
        ok, err = _probe(enc, [])
        if not ok:
            result["rejected"][enc] = err
            continue
        result["encoder"] = enc
        result["label"] = LABELS.get(enc, enc)
        result["ten_bit"] = _probe(enc, [], ten_bit=True)[0]
        result["features"] = {
            name: _probe(enc, args)[0]
            for name, args in _FEATURE_PROBES.get(enc, {}).items()
        }
        break

    return _store(result)


def _read_cache():
    try:
        with open(HWCACHE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _store(result):
    global _cache
    _cache = result
    try:
        ensure_data_dirs()
        tmp = HWCACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f, indent=1)
        os.replace(tmp, HWCACHE_PATH)
    except OSError:
        pass
    return result


def encoder():
    """Name of the usable hardware HEVC encoder, or None."""
    return detect()["encoder"]


def supports_10bit():
    return bool(detect()["ten_bit"])


def features():
    return dict(detect()["features"])


def summary(force=False):
    """Human-readable state for the UI and setup scripts."""
    d = detect(force=force)
    if d["encoder"]:
        bits = "8/10-bit" if d["ten_bit"] else "8-bit only"
        return {
            "available": True,
            "encoder": d["encoder"],
            "label": d["label"],
            "ten_bit": d["ten_bit"],
            "features": d["features"],
            "detail": f"{d['label']} ({d['encoder']}, {bits})",
            "rejected": d["rejected"],
        }
    return {
        "available": False,
        "encoder": None,
        "label": None,
        "ten_bit": False,
        "features": {},
        "detail": _explain(d),
        "rejected": d["rejected"],
    }


_NVENC_API = re.compile(r"nvenc API version.*Required:\s*([\d.]+).*Found:\s*([\d.]+)", re.I)


def _explain(d):
    """Turn probe failures into something actionable."""
    for enc, err in d["rejected"].items():
        m = _NVENC_API.search(err)
        if m:
            return (f"NVENC unavailable: this ffmpeg needs NVENC API {m.group(1)} "
                    f"but the driver provides {m.group(2)}. Install an older "
                    f"ffmpeg (8.0 works) or update the NVIDIA driver.")
    if d["rejected"].get("*"):
        return "Hardware encoding disabled by VIDSQUEEZE_HW_ENCODER."
    return "No usable hardware HEVC encoder found; the fast profile will use libx265."


if __name__ == "__main__":
    info = detect(force=True)
    print(json.dumps(info, indent=2))
