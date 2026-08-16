"""Synthetic end-to-end test of the compress pipeline (no phone needed).

Generates test clips that mimic S24 Ultra footage (4K60, portrait-with-rotation,
10-bit HDR), runs the real pipeline functions on them, and asserts that
dimensions, fps, duration, dates, GPS and file mtime all survive.

Runs on macOS, Windows and Linux: the "fast" profile uses whatever hardware
encoder this machine actually has, and falls back to libx265 if it has none.

Run:  .venv/bin/python tests/test_pipeline.py       (Windows: .venv\\Scripts\\python)
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hwaccel, metadata  # noqa: E402
from app.config import NO_WINDOW, FFMPEG, FFPROBE, EXIFTOOL  # noqa: E402
from app.encoder import (EncodeOptions, Encoder, build_command,  # noqa: E402
                         quality_to_cq, resolve_hw)
from app.probe import ProbeResult, probe  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
MTIME = 1717236000  # 2024-06-01 10:00:00 UTC
CREATION = "2024-06-01T10:00:00.000000Z"
LOCATION = "+12.9716+077.5946/"

failures = []


def check(name, cond, detail=""):
    status = "ok " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(f"{name}: {detail}")


def run(cmd, **kw):
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    r = subprocess.run(cmd, capture_output=True, text=True, **NO_WINDOW, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stderr[-800:]}")
    return r


def gen_source(path, size, rate, extra_video_args, ten_bit=False):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
           "-f", "lavfi", "-i", "sine=frequency=440",
           "-t", "2", "-shortest",
           "-metadata", f"creation_time={CREATION}",
           "-metadata", f"location={LOCATION}",
           *extra_video_args,
           "-c:a", "aac", "-b:a", "96k",
           path]
    run(cmd)
    os.utime(path, (MTIME, MTIME))


def exif_json(path):
    r = run([EXIFTOOL, "-j", "-n", "-api", "LargeFileSupport=1", path])
    return json.loads(r.stdout)[0]


def ffprobe_format_tags(path):
    r = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", path])
    return json.loads(r.stdout).get("format", {}).get("tags", {})


def compress(src, dst, opts):
    pr = probe(src)
    enc = Encoder()
    log = os.path.join(TMP, "encode.log")
    t0 = time.time()
    enc.run(src, dst, opts, pr, log)
    metadata.copy_all_tags(src, dst)
    metadata.restore_file_times(src, dst)
    return pr, probe(dst), time.time() - t0


def common_checks(label, src, dst, pr_in, pr_out):
    check(f"{label}: duration preserved", abs(pr_out.duration - pr_in.duration) <= 1.0,
          f"{pr_in.duration} -> {pr_out.duration}")
    check(f"{label}: fps preserved", abs(pr_out.fps - pr_in.fps) < 0.5,
          f"{pr_in.fps} -> {pr_out.fps}")
    check(f"{label}: HEVC output", pr_out.video_codec == "hevc", pr_out.video_codec)
    check(f"{label}: file mtime preserved", abs(os.path.getmtime(dst) - MTIME) <= 1,
          str(os.path.getmtime(dst)))
    tags = ffprobe_format_tags(dst)
    ct = tags.get("creation_time", "")
    check(f"{label}: creation_time preserved", ct.startswith("2024-06-01T10:00:00"), ct)
    loc = tags.get("location", "") or tags.get("location-eng", "")
    check(f"{label}: GPS location preserved", loc.startswith("+12.9716+077.594"), loc)
    ex = exif_json(dst)
    cd = str(ex.get("CreateDate", ""))
    check(f"{label}: exif CreateDate preserved", cd.startswith("2024:06:01 10:00:00"), cd)


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    hw = hwaccel.summary()
    print(f"platform: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"fast profile: {hw['detail']}")
    print()
    fast = EncodeOptions(profile="fast", hw_quality=55, max_long_edge=1920)
    quality = EncodeOptions(profile="quality", crf=32, preset="medium", max_long_edge=1920)

    print("== 1. Landscape 4K60 → fast profile (scale + fps + metadata) ==")
    src = os.path.join(TMP, "land4k.mp4")
    gen_source(src, "3840x2160", 60, ["-c:v", "libx264", "-preset", "ultrafast",
                                      "-pix_fmt", "yuv420p"])
    dst = os.path.join(TMP, "out_land4k.mp4")
    pr_in, pr_out, secs = compress(src, dst, fast)
    print(f"  encoded in {secs:.1f}s, {os.path.getsize(src)} -> {os.path.getsize(dst)} bytes")
    check("land4k: scaled to 1920x1080", (pr_out.width, pr_out.height) == (1920, 1080),
          f"{pr_out.width}x{pr_out.height}")
    common_checks("land4k", src, dst, pr_in, pr_out)

    print("== 2. Portrait (4K + rotation metadata) → fast profile ==")
    src_p = os.path.join(TMP, "portrait.mp4")
    run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-display_rotation", "90", "-i", src, "-c", "copy", src_p])
    os.utime(src_p, (MTIME, MTIME))
    pr_src_p = probe(src_p)
    check("portrait: source has rotation", pr_src_p.rotation % 180 != 0,
          f"rotation={pr_src_p.rotation}")
    dst_p = os.path.join(TMP, "out_portrait.mp4")
    pr_in, pr_out, secs = compress(src_p, dst_p, fast)
    print(f"  encoded in {secs:.1f}s")
    check("portrait: output is 1080x1920", (pr_out.width, pr_out.height) == (1080, 1920),
          f"{pr_out.width}x{pr_out.height}")
    check("portrait: no rotation tag on output", pr_out.rotation % 360 == 0,
          f"rotation={pr_out.rotation}")
    ex = exif_json(dst_p)
    check("portrait: exif Rotation not re-applied", ex.get("Rotation", 0) in (0, None),
          str(ex.get("Rotation")))

    print("== 3. 10-bit HDR 720p → quality profile (x265, no upscale) ==")
    src_h = os.path.join(TMP, "hdr.mp4")
    gen_source(src_h, "1280x720", 30,
               ["-c:v", "libx265", "-preset", "ultrafast", "-crf", "30",
                "-vf", "format=yuv420p10le,"
                       "setparams=color_primaries=bt2020:color_trc=smpte2084:"
                       "colorspace=bt2020nc",
                "-x265-params", "log-level=error", "-tag:v", "hvc1"])
    dst_h = os.path.join(TMP, "out_hdr.mp4")
    pr_in, pr_out, secs = compress(src_h, dst_h, quality)
    print(f"  encoded in {secs:.1f}s")
    check("hdr: source detected as HDR", pr_in.is_hdr, pr_in.color_transfer)
    check("hdr: output stays 10-bit", pr_out.is_10bit, pr_out.pix_fmt)
    check("hdr: transfer preserved", pr_out.color_transfer == "smpte2084",
          pr_out.color_transfer)
    check("hdr: primaries preserved", pr_out.color_primaries == "bt2020",
          pr_out.color_primaries)
    check("hdr: not upscaled", (pr_out.width, pr_out.height) == (1280, 720),
          f"{pr_out.width}x{pr_out.height}")
    common_checks("hdr", src_h, dst_h, pr_in, pr_out)

    print("== 4. SDR 720p30 → quality profile (x265 8-bit path) ==")
    src_s = os.path.join(TMP, "sdr720.mp4")
    gen_source(src_s, "1280x720", 30, ["-c:v", "libx264", "-preset", "ultrafast",
                                       "-pix_fmt", "yuv420p"])
    dst_s = os.path.join(TMP, "out_sdr720.mp4")
    pr_in, pr_out, secs = compress(src_s, dst_s, quality)
    print(f"  encoded in {secs:.1f}s")
    check("sdr720: output is 8-bit", not pr_out.is_10bit, pr_out.pix_fmt)
    common_checks("sdr720", src_s, dst_s, pr_in, pr_out)

    print("== 5. 10-bit HDR → fast profile (hardware 10-bit, or software fallback) ==")
    # The one thing that must never happen to HDR footage is a silent drop to
    # 8-bit, which washes the colours out. Whichever encoder the fast profile
    # resolves to, the output has to stay 10-bit.
    dst_hw = os.path.join(TMP, "out_hdr_fast.mp4")
    pr_in, pr_out, secs = compress(src_h, dst_hw, fast)
    print(f"  encoded in {secs:.1f}s, {os.path.getsize(src_h)} -> "
          f"{os.path.getsize(dst_hw)} bytes")
    check("hdr-fast: output stays 10-bit", pr_out.is_10bit, pr_out.pix_fmt)
    check("hdr-fast: transfer preserved", pr_out.color_transfer == "smpte2084",
          pr_out.color_transfer)
    check("hdr-fast: primaries preserved", pr_out.color_primaries == "bt2020",
          pr_out.color_primaries)
    common_checks("hdr-fast", src_h, dst_hw, pr_in, pr_out)

    print("== 6. Encoder argument mapping (all platforms, no encoding) ==")
    # Guards the cross-platform contract without needing the hardware present.
    sdr = ProbeResult(3840, 2160, 10.0, 60.0, "hevc", "yuv420p", 8,
                      "bt709", "bt709", "bt709", 50_000_000, 0)
    hdr10 = ProbeResult(3840, 2160, 10.0, 60.0, "hevc", "yuv420p10le", 10,
                        "bt2020", "smpte2084", "bt2020nc", 80_000_000, 0)
    check("cq mapping is monotonic and inverted",
          quality_to_cq(30) > quality_to_cq(55) > quality_to_cq(80),
          f"{quality_to_cq(30)} / {quality_to_cq(55)} / {quality_to_cq(80)}")

    expectations = {
        # encoder            quality flag        8-bit fmt   10-bit fmt
        "hevc_videotoolbox": ("-q:v", "55",      "yuv420p",  "p010le"),
        "hevc_nvenc":        ("-cq", "29",       "yuv420p",  "p010le"),
        "hevc_qsv":          ("-global_quality", "29", "nv12", "p010le"),
        "hevc_amf":          ("-qp_i", "29",     "nv12",     "p010le"),
    }
    for enc, (flag, val, fmt8, fmt10) in expectations.items():
        opts = EncodeOptions(profile="fast", hw_quality=55, hw_encoder=enc)
        c8 = build_command("i.mp4", "o.mp4", opts, sdr)
        c10 = build_command("i.mp4", "o.mp4", opts, hdr10)
        check(f"{enc}: selected", "-c:v" in c8 and c8[c8.index("-c:v") + 1] == enc)
        check(f"{enc}: quality flag {flag}={val}",
              flag in c8 and c8[c8.index(flag) + 1] == val,
              " ".join(c8[c8.index("-c:v"):c8.index("-c:a")]))
        check(f"{enc}: 8-bit pixel format", fmt8 in c8,
              " ".join(c8[c8.index("-c:v"):c8.index("-c:a")]))
        # An encoder that cannot do 10-bit is expected to fall back to libx265
        # rather than quietly truncating HDR to 8 bits — either way the output
        # must stay 10-bit.
        _, hw10, _ = resolve_hw(opts)
        want = (fmt10, "main10") if hw10 else ("yuv420p10le",)
        check(f"{enc}: 10-bit path ({'hardware' if hw10 else 'software fallback'})",
              all(w in c10 for w in want),
              " ".join(c10[c10.index("-c:v"):c10.index("-c:a")]))

    # NVENC ignores -cq unless the VBR target bitrate is explicitly zeroed.
    nv = build_command("i.mp4", "o.mp4",
                       EncodeOptions(profile="fast", hw_encoder="hevc_nvenc"), sdr)
    check("hevc_nvenc: -b:v 0 present so -cq is honoured",
          "-b:v" in nv and nv[nv.index("-b:v") + 1] == "0", " ".join(nv))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All checks passed ✅")


if __name__ == "__main__":
    main()
