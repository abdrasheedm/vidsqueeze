# VidSqueeze

Shrink the videos on your Android phone — **without changing anything the gallery
cares about**. Same filename, recording date, GPS location and file timestamps, so
your gallery timeline stays exactly as it was. Only the file sizes drop.

Nothing is ever written back to the phone until **you** approve it.

Runs on macOS, Windows and Linux, using whatever hardware video encoder the
machine actually has.

## How it works

The workflow is split into three phases, so the phone only needs to be connected
for the first and last one:

| Phase | Phone needed? | What happens |
|---|---|---|
| **1 · Select & pull** | Yes | Browse videos by folder with thumbnails, pick what you want, copy originals to the computer |
| **2 · Compress** | No | Encode everything offline — leave it running for hours, unplugged |
| **3 · Review & approve** | Only to push | Compare original vs compressed side by side, approve what you like, push back |

Because compressing happens offline, a 500-video batch doesn't mean 8 hours tethered
to a cable. Pull in the morning, compress during the day, approve and push whenever.

## Setup

**macOS / Linux (once):**

```bash
./setup.sh        # installs ffmpeg, exiftool and adb, creates the venv
```

**Windows (once):**

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

or just double-click `setup.bat`. It installs ffmpeg, exiftool and adb via winget
(falling back to scoop), creates the venv, and then **verifies that hardware
encoding really works** — see [Hardware encoding](#hardware-encoding) for why that
last step matters.

**Phone (once):**

1. Settings → About phone → Software information → tap **Build number** 7 times.
2. Settings → Developer options → enable **USB debugging**.
3. Connect the cable, tap **Allow** ("Always allow from this computer").

## Usage

```bash
./run.sh                                              # macOS / Linux
powershell -ExecutionPolicy Bypass -File .\run.ps1    # Windows (or run.bat)
```

Opens http://127.0.0.1:8765.

**Folder mode** works with no phone at all: point it at any folder on the computer,
compress, and write the results to an output folder of your choice.

### Comparing quality

Click **Compare** on any compressed video to open the A/B viewer:

- **Split** — drag the divider across the frame to see the exact same pixels before and after.
- **Side by side** — both videos playing in sync, at equal size.
- Audio toggle so you can hear the original vs the compressed track (one at a time).
- Frame-step buttons and keyboard shortcuts: `space` play/pause, `←`/`→` step a frame,
  `A`/`B` switch audio, `esc` to close.

## Safety

- **Nothing is deleted, ever.** Both the original and the compressed copy stay in
  `VideoCompressor/library/<item>/` until you delete them yourself.
- A file on the phone is only replaced after you approve it, and only if the compressed
  version passed verification (duration matches, file is actually smaller).
- If compressing would make a file *bigger*, it's marked "kept original" and the phone
  copy is left alone.
- Quitting the app or unplugging mid-batch loses nothing — state is stored per item on
  disk and picked up where it left off.
- Before a pull starts, free space is checked for **both** copies plus headroom, and
  the batch is refused rather than half-run.

## Encoder profiles

- **Quality (x265)** — best compression, slow. This is what turned 32 GB into ~2 GB.
- **Fast (hardware)** — whatever hardware encoder this machine has, several times
  faster, with larger files.

The fast profile is not tied to one platform. At startup the app looks for a usable
HEVC encoder in this order and uses the first one that survives a real test encode:

| Platform | Tried |
|---|---|
| macOS | `hevc_videotoolbox` |
| Windows | `hevc_nvenc` → `hevc_qsv` → `hevc_amf` |
| Linux | `hevc_nvenc` → `hevc_qsv` → `hevc_vaapi` |

If none work, the fast profile falls back to `libx265 -preset veryfast`, so it always
produces a correct file — just not a fast one.

4K is downscaled to 1080p by default (long edge 1920, portrait stays portrait) while the
frame rate is left untouched. HDR / 10-bit videos are detected and re-encoded in 10-bit
with their color metadata preserved; if the hardware encoder can't do 10-bit, that clip
goes to libx265 rather than being silently flattened to 8-bit.

### The quality slider

Hardware encoders don't share a quality scale. VideoToolbox takes `-q:v` on a 1–100
scale where *higher* is better; NVENC, Quick Sync and AMF take a CRF-like 0–51 value
where *lower* is better. The UI exposes one 1–100 "higher is better" number and each
encoder maps it:

| Encoder | Flag | Slider 30 / 55 / 80 |
|---|---|---|
| `hevc_videotoolbox` | `-q:v` | 30 / 55 / 80 |
| `hevc_nvenc` | `-cq` (+ `-b:v 0`) | 39 / 29 / 19 |
| `hevc_qsv` | `-global_quality` | 39 / 29 / 19 |
| `hevc_amf` | `-qp_i` / `-qp_p` | 39 / 29 / 19 |

`-b:v 0` is not decoration: without it NVENC's VBR mode clamps to a default bitrate and
ignores `-cq` entirely.

## Hardware encoding

`ffmpeg -encoders` lists what the binary was **built** with, not what your machine can
run. A stock Windows ffmpeg advertises NVENC, Quick Sync *and* AMF on a laptop that has
only one of them — and even a real NVIDIA GPU fails if the ffmpeg build wants a newer
NVENC API than the installed driver provides:

```
Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0
```

So the app never trusts the list. `app/hwaccel.py` runs a real five-frame encode with
each candidate, plus separate probes for 10-bit and for optional features (B-frames,
spatial/temporal AQ, multipass), and caches the answer. The cache is keyed on the ffmpeg
path and version, so swapping ffmpeg re-runs the probes automatically.

If NVENC is rejected for the API-version reason above, `setup.ps1` automatically retries
with a pinned older ffmpeg (8.0, then 7.1.1). You can also do it by hand:

```powershell
winget install --id Gyan.FFmpeg -e --version 8.0 --force
```

To inspect what was detected:

```bash
.venv/bin/python -m app.hwaccel          # Windows: .venv\Scripts\python -m app.hwaccel
```

Environment overrides:

| Variable | Effect |
|---|---|
| `VIDSQUEEZE_HW_ENCODER` | Force an encoder, or `none` to disable hardware encoding |
| `VIDSQUEEZE_DATA_DIR` | Where originals, compressed copies, logs and thumbnails live |
| `VIDSQUEEZE_FFMPEG` / `_FFPROBE` / `_EXIFTOOL` / `_ADB` | Point at a specific binary |

## Layout

Data lives in `VideoCompressor/` next to the app, or wherever `VIDSQUEEZE_DATA_DIR`
points:

```
VideoCompressor/
  library/<item-id>/original.mp4     # your original, kept
  library/<item-id>/compressed.mp4   # the result, kept
  library/<item-id>/meta.json        # state, sizes, probe data
  logs/                              # per-run ffmpeg logs
  hwaccel.json                       # cached encoder probe results
```

A batch holds both copies of everything, so budget roughly **2.2×** the size of the
videos you plan to pull. Point `VIDSQUEEZE_DATA_DIR` at an external drive for large
batches.

> Upgrading from an older version? Data used to live in `~/VideoCompressor`. Either move
> that folder next to the app, or set `VIDSQUEEZE_DATA_DIR=~/VideoCompressor`.

## Tests

```bash
.venv/bin/python tests/test_pipeline.py         # metadata, HDR, portrait, GPS preservation
.venv/bin/python tests/test_review_flow.py      # approval gate, resume, folder mode
```

On Windows use `.venv\Scripts\python` instead. Both suites run on every platform; the
fast-profile cases use whichever encoder was detected, and `test_pipeline.py` also
checks the argument mapping for *all* encoders without needing that hardware present.

To compare encoders on your own footage:

```bash
.venv/bin/python tests/benchmark_encoders.py clip1.mp4 clip2.mp4
```

It encodes each clip with libx265 and the hardware encoder at several quality settings
and reports size, wall time and VMAF against the same downscaled reference.

## License

MIT — see [LICENSE](LICENSE).

This tool drives `ffmpeg`, `exiftool` and `adb` as external command-line programs;
each carries its own license and is installed separately by the setup script.
