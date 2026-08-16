# VidSqueeze

Shrink the videos on your Android phone from your Mac — **without changing anything
the gallery cares about**. Same filename, recording date, GPS location and file
timestamps, so your gallery timeline stays exactly as it was. Only the file sizes drop.

Nothing is ever written back to the phone until **you** approve it.

## How it works

The workflow is split into three phases, so the phone only needs to be connected
for the first and last one:

| Phase | Phone needed? | What happens |
|---|---|---|
| **1 · Select & pull** | Yes | Browse videos by folder with thumbnails, pick what you want, copy originals to the Mac |
| **2 · Compress** | No | Encode everything offline — leave it running for hours, unplugged |
| **3 · Review & approve** | Only to push | Compare original vs compressed side by side, approve what you like, push back |

Because compressing happens offline, a 500-video batch doesn't mean 8 hours tethered
to a cable. Pull in the morning, compress during the day, approve and push whenever.

## Setup

**Mac (once):**

```bash
./setup.sh        # installs ffmpeg, exiftool and adb via Homebrew, creates the venv
```

**Phone (once):**

1. Settings → About phone → Software information → tap **Build number** 7 times.
2. Settings → Developer options → enable **USB debugging**.
3. Connect the cable, tap **Allow** ("Always allow from this computer").

## Usage

```bash
./run.sh          # opens http://127.0.0.1:8765
```

**Folder mode** works with no phone at all: point it at any folder on your Mac,
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
  `~/VideoCompressor/library/<item>/` until you delete them yourself.
- A file on the phone is only replaced after you approve it, and only if the compressed
  version passed verification (duration matches, file is actually smaller).
- If compressing would make a file *bigger*, it's marked "kept original" and the phone
  copy is left alone.
- Quitting the app or unplugging mid-batch loses nothing — state is stored per item on
  disk and picked up where it left off.

## Encoder profiles

- **Quality (x265)** — best compression, slow. This is what turned 32 GB into ~2 GB.
- **Fast (hardware)** — Apple's VideoToolbox encoder, roughly 10× faster and cooler on a
  fanless Mac, with files about 1.5–2× larger.

4K is downscaled to 1080p by default (long edge 1920, portrait stays portrait) while the
frame rate is left untouched. HDR / 10-bit videos are detected and re-encoded in 10-bit
with their color metadata preserved.

## Layout

```
~/VideoCompressor/
  library/<item-id>/original.mp4     # your original, kept
  library/<item-id>/compressed.mp4   # the result, kept
  library/<item-id>/meta.json        # state, sizes, probe data
  logs/                              # per-run ffmpeg logs
```

## Tests

```bash
.venv/bin/python tests/test_pipeline.py      # metadata, HDR, portrait, GPS preservation
.venv/bin/python tests/test_review_flow.py   # approval gate, resume, folder mode
```

## License

MIT — see [LICENSE](LICENSE).

This tool drives `ffmpeg`, `exiftool` and `adb` as external command-line programs;
each carries its own license and is installed separately by `setup.sh`.
