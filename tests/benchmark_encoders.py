"""Compare the hardware encoder against libx265 on real clips.

Encodes each source at several quality settings with both encoders, measures
VMAF against the *same* downscaled reference, and prints size / speed / quality
side by side — enough to pick a profile for a large batch.

The reference is the source scaled on the fly with the exact filter the
pipeline uses, so the numbers isolate encoder quality from the downscale.

Run:  .venv/Scripts/python tests/benchmark_encoders.py clip1.mp4 clip2.mp4
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hwaccel  # noqa: E402
from app.config import FFMPEG, NO_WINDOW  # noqa: E402
from app.encoder import EncodeOptions, build_command  # noqa: E402
from app.probe import probe  # noqa: E402


def scale_expr(long_edge):
    return (f"scale=w='min(iw,{long_edge})':h='min(ih,{long_edge})':"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **NO_WINDOW, **kw)


def encode(src, dst, opts, pr):
    cmd = build_command(src, dst, opts, pr)
    cmd = [a for a in cmd if a not in ("-progress", "pipe:1")]
    t0 = time.time()
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-500:])
    return time.time() - t0


def encode_cuda(src, dst, cq, long_edge, ten_bit):
    """NVENC with CUDA decode + GPU scaling: no frames cross the PCIe bus."""
    fmt = "p010le" if ten_bit else "yuv420p"
    cmd = [FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", src,
           "-map_metadata", "0", "-map_chapters", "0",
           "-vf", f"scale_cuda=w={long_edge}:h=-2:force_original_aspect_ratio=decrease:"
                  f"format={fmt}",
           "-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq",
           "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-bf", "3",
           "-spatial-aq", "1", "-temporal-aq", "1",
           "-profile:v", "main10" if ten_bit else "main",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-tag:v", "hvc1", dst]
    t0 = time.time()
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return time.time() - t0


_VMAF_RE = re.compile(r'"?VMAF score"?\s*[:=]\s*([\d.]+)')


def vmaf(dist, ref_src, long_edge, ten_bit):
    """VMAF of `dist` against `ref_src` scaled the same way the encode was."""
    fmt = "yuv420p10le" if ten_bit else "yuv420p"
    out_json = dist + ".vmaf.json"
    lavfi = (f"[1:v]{scale_expr(long_edge)},format={fmt},setpts=PTS-STARTPTS[ref];"
             f"[0:v]format={fmt},setpts=PTS-STARTPTS[dist];"
             f"[dist][ref]libvmaf=log_fmt=json:log_path="
             f"{out_json.replace(chr(92), '/').replace(':', chr(92) + ':')}")
    r = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-i", dist, "-i", ref_src, "-lavfi", lavfi, "-f", "null", "-"])
    try:
        with open(out_json) as f:
            data = json.load(f)
        return float(data["pooled_metrics"]["vmaf"]["mean"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        m = _VMAF_RE.search(r.stderr or "")
        if m:
            return float(m.group(1))
        print(f"    [vmaf failed] {(r.stderr or '').strip()[-300:]}")
        return None
    finally:
        try:
            os.remove(out_json)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--long-edge", type=int, default=1920)
    ap.add_argument("--crf", default="28,32,36")
    ap.add_argument("--cq", default="24,29,34,39")
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--outdir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tmp_bench"))
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    hw = hwaccel.summary()
    print(f"hardware encoder: {hw['detail']}\n")

    all_rows = []
    for clip in args.clips:
        pr = probe(clip)
        ten_bit = pr.is_10bit or pr.is_hdr
        src_mb = os.path.getsize(clip) / 1024**2
        print(f"== {os.path.basename(clip)} ==")
        print(f"   {pr.width}x{pr.height} @ {pr.fps}fps, {pr.duration:.1f}s, "
              f"{src_mb:.1f} MB, {pr.pix_fmt}"
              f"{', HDR ' + pr.color_transfer if pr.is_hdr else ''}")

        jobs = []
        for crf in [int(x) for x in args.crf.split(",")]:
            jobs.append((f"libx265 crf{crf} {args.preset}",
                         EncodeOptions(profile="quality", crf=crf,
                                       preset=args.preset,
                                       max_long_edge=args.long_edge), None))
        for cq in [int(x) for x in args.cq.split(",")]:
            # hw_quality is the UI scale; drive -cq directly here instead.
            opts = EncodeOptions(profile="fast", max_long_edge=args.long_edge)
            jobs.append((f"nvenc cq{cq}", opts, cq))

        rows = []
        for label, opts, cq in jobs:
            dst = os.path.join(args.outdir,
                               re.sub(r"\W+", "_", f"{os.path.basename(clip)}_{label}") + ".mp4")
            try:
                if cq is not None:
                    from app import encoder as enc_mod
                    orig = enc_mod.quality_to_cq
                    enc_mod.quality_to_cq = lambda _q, _c=cq: _c
                    try:
                        secs = encode(clip, dst, opts, pr)
                    finally:
                        enc_mod.quality_to_cq = orig
                else:
                    secs = encode(clip, dst, opts, pr)
            except RuntimeError as e:
                print(f"   {label:<24} FAILED: {e}")
                continue
            mb = os.path.getsize(dst) / 1024**2
            score = vmaf(dst, clip, args.long_edge, ten_bit)
            speed = pr.duration / secs if secs else 0
            rows.append({"clip": os.path.basename(clip), "label": label,
                         "mb": mb, "secs": secs, "speed": speed,
                         "vmaf": score, "src_mb": src_mb})
            print(f"   {label:<24} {mb:7.1f} MB  {secs:6.1f}s "
                  f"({speed:5.2f}x realtime)  VMAF "
                  f"{score:.2f}" if score is not None else
                  f"   {label:<24} {mb:7.1f} MB  {secs:6.1f}s  VMAF n/a")

        # Does keeping the whole pipeline on the GPU help throughput?
        if hw.get("encoder") == "hevc_nvenc":
            dst = os.path.join(args.outdir, "cudadec_" +
                               re.sub(r"\W+", "_", os.path.basename(clip)) + ".mp4")
            try:
                secs = encode_cuda(clip, dst, 29, args.long_edge, ten_bit)
                mb = os.path.getsize(dst) / 1024**2
                print(f"   {'nvenc cq29 + CUDA decode':<24} {mb:7.1f} MB  "
                      f"{secs:6.1f}s ({pr.duration / secs:5.2f}x realtime)")
            except RuntimeError as e:
                print(f"   {'nvenc + CUDA decode':<24} n/a: {str(e)[:120]}")

        all_rows += rows
        print()

    with open(os.path.join(args.outdir, "results.json"), "w") as f:
        json.dump(all_rows, f, indent=1)
    print(f"raw results: {os.path.join(args.outdir, 'results.json')}")


if __name__ == "__main__":
    main()
