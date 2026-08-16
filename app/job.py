"""Phase runners: Pull (phone), Encode (offline), Push (phone).

Each phase is independent and restartable. Item state lives in library/meta.json,
so quitting the app or unplugging the phone never loses work. Nothing is ever
pushed to the phone without an explicit approval.
"""
import os
import shutil
import threading
import time

from . import library, metadata, phone, thumbs
from .config import LIBRARY_DIR, LOG_DIR, ensure_data_dirs
from .encoder import EncodeCancelled, EncodeOptions, Encoder
from .probe import probe

DURATION_TOLERANCE_S = 1.0
MAX_CONSECUTIVE_FAILURES = 3
PROGRESS_POLL_S = 0.4
COPY_CHUNK = 4 * 1024 * 1024


def _rate(done, t0):
    elapsed = time.time() - t0
    return f"{done / elapsed / 1024**2:.1f} MB/s" if elapsed >= 1.0 else ""


def copy_with_progress(src, dst, phase):
    """shutil.copy2 that reports progress as it goes.

    Not implemented by watching the file grow: shutil's fast path on Windows
    preallocates the destination to its final size, so the file is 100% of its
    eventual length from the first moment. Copying in chunks lets us count the
    bytes actually written.
    """
    total = max(os.path.getsize(src), 1)
    done = 0
    t0 = time.time()
    phase.progress = 0.0
    phase.speed = ""
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                chunk = fsrc.read(COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                done += len(chunk)
                phase.progress = min(done / total, 1.0)
                phase.speed = _rate(done, t0)
        shutil.copystat(src, dst)
    finally:
        # Zero it as the completed counter ticks up, so the bar stays smooth.
        phase.progress = 0.0
        phase.speed = ""


class _CopyProgress:
    """Report progress for a copy performed by an external process (adb).

    adb doesn't report progress in any parseable way, but the file it is
    writing does: comparing its size against the size we already know from the
    phone listing gives a live percentage and transfer rate.
    """

    def __init__(self, phase, path, expected_bytes):
        self.phase = phase
        self.path = path
        self.expected = max(int(expected_bytes or 0), 1)
        self._stop = threading.Event()
        self._thread = None
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.time()
        self.phase.progress = 0.0
        self.phase.speed = ""
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _watch(self):
        while not self._stop.wait(PROGRESS_POLL_S):
            try:
                done = os.path.getsize(self.path)
            except OSError:
                continue          # not created yet, or momentarily unreadable
            self.phase.progress = min(done / self.expected, 1.0)
            self.phase.speed = _rate(done, self._t0)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # Zero it as the completed counter ticks up, so the bar stays smooth.
        self.phase.progress = 0.0
        self.phase.speed = ""
        return False


class Phase:
    """Base for a background phase operating over library items."""

    kind = "phase"

    # Fixed wall-clock cost per item, on top of the size-dependent part:
    # spawning adb, stat-ing the remote file, writing meta.json. Measured over a
    # real 340-file / 20.4 GB pull: 820s total against ~565s of actual transfer
    # at the observed 37 MB/s, so roughly 0.75s each - a third of the run.
    # Ignoring it makes a batch of many small files finish far later than the
    # estimate promised.
    item_overhead_s = 0.75

    def __init__(self, item_ids, opts=None):
        ensure_data_dirs()
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.item_ids = list(item_ids)
        self.opts = opts or EncodeOptions()
        self.state = "running"
        self.error = None
        self.started = int(time.time())
        self.finished = None
        self.current = None
        self.done_ids = []
        # Progress within the file being handled right now (0..1), for phases
        # that aren't running an encoder.
        self.progress = 0.0
        self.speed = ""
        # Set while pausing between batches, so the UI can say so rather than
        # looking stalled.
        self.resting = False
        self.rest_left = 0
        # Work accounting for the ETA. Counting files would be badly wrong when
        # they vary from 2 MB to 900 MB, so each phase weights an item by
        # whatever actually predicts its cost - see _weight().
        self._weights = {}
        self._total_weight = 0.0
        self._done_weight = 0.0
        self._attempted_items = 0
        self._rested_seconds = 0.0
        self.log_path = os.path.join(LOG_DIR, f"{self.id}-{self.kind}.log")
        self._cancel = threading.Event()
        self._encoder = None
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def cancel(self):
        self._cancel.set()
        with self._lock:
            if self._encoder:
                self._encoder.cancel()

    def _weight(self, meta):
        """Cost of one item, in whatever unit predicts its time best.

        Copying is bandwidth-bound, so bytes. EncodePhase overrides this.
        """
        return max(float(meta.get("original_size") or 0), 1.0)

    def _prepare_weights(self):
        for item_id in self.item_ids:
            meta = library.load(item_id)
            if meta:
                self._weights[item_id] = max(self._weight(meta), 1e-6)
        self._total_weight = sum(self._weights.values())

    def eta_seconds(self):
        """Seconds left at the rate achieved so far, or None if not yet knowable.

        Time is modelled as (work / rate) + (items * fixed overhead) rather than
        a single average, because the two scale differently: a batch of 300
        small clips pays the per-item cost 300 times while moving very few
        bytes. The rate is measured live; the overhead is a per-phase constant.

        Rest periods are excluded, so pausing between batches doesn't drag the
        measured rate down.
        """
        if self.state != "running" or self._total_weight <= 0:
            return None
        progress = self._encoder.progress if self._encoder else self.progress
        done = self._done_weight + (self._weights.get(self.current, 0.0)
                                    * min(max(progress, 0.0), 1.0))
        elapsed = time.time() - self.started - self._rested_seconds
        # The item in flight has already paid its overhead.
        working = elapsed - self._attempted_items * self.item_overhead_s
        # One item's timing is mostly noise - a first guess drawn from it came
        # out ~2x wrong in testing, then settled within a second once a couple
        # of items had finished. Better to show nothing for those few seconds
        # than a number that visibly lurches.
        settled = len(self.done_ids) >= 2 or working >= 8.0
        if done <= 0 or working < 2.0 or not settled:
            return None
        rate = done / working
        if rate <= 0:
            return None
        remaining_items = max(len(self.item_ids) - self._attempted_items, 0)
        return int(max(self._total_weight - done, 0.0) / rate
                   + remaining_items * self.item_overhead_s)

    def _log(self, msg):
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def _run(self):
        failures = 0
        self._prepare_weights()
        try:
            for attempted, item_id in enumerate(self.item_ids, start=1):
                if self._cancel.is_set():
                    break
                meta = library.load(item_id)
                if meta is None:
                    continue
                self.current = item_id
                self._attempted_items += 1
                try:
                    self.handle(meta)
                    self.done_ids.append(item_id)
                    failures = 0
                except EncodeCancelled:
                    library.update(item_id, state=self.resume_state, error=None)
                    break
                except Exception as e:                      # noqa: BLE001
                    msg = str(e)[:500]
                    self._log(f"FAILED {meta['name']}: {msg}")
                    library.update(item_id, state="failed", error=msg)
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        self.error = f"stopped after {failures} consecutive failures"
                        self._log(self.error)
                        break
                finally:
                    # Count attempted work whatever the outcome, or a failure
                    # would leave the estimate permanently short.
                    self._done_weight += self._weights.get(item_id, 0.0)
                self.current = None
                self._rest_between_batches(attempted)
        finally:
            self.current = None
            self.finished = int(time.time())
            self.state = ("failed" if self.error else
                          "cancelled" if self._cancel.is_set() else "done")
            self._log(f"{self.kind} {self.state}")

    def snapshot(self):
        return {
            "kind": self.kind,
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "current": self.current,
            "total": len(self.item_ids),
            "completed": len(self.done_ids),
            "progress": (self._encoder.progress if self._encoder else self.progress),
            "speed": (self._encoder.speed if self._encoder else self.speed),
            "resting": self.resting,
            "rest_left": self.rest_left,
            "eta": self.eta_seconds(),
        }

    def _rest_between_batches(self, attempted):
        """Idle for a while every N files so a long run doesn't cook the machine.

        Encoding thousands of clips back to back pins the GPU and CPU for hours;
        on a laptop that means sustained heat and throttling. Pausing between
        batches lets everything cool down. Off unless the caller asks for it.
        """
        size = getattr(self.opts, "batch_size", 0) or 0
        secs = getattr(self.opts, "rest_seconds", 0) or 0
        if size <= 0 or secs <= 0:
            return
        if attempted % size or attempted >= len(self.item_ids):
            return                      # mid-batch, or nothing left to do
        self._log(f"resting {secs}s after {attempted} files")
        started_rest = time.time()
        deadline = started_rest + secs
        self.resting = True
        try:
            while not self._cancel.is_set():
                left = deadline - time.time()
                if left <= 0:
                    break
                self.rest_left = int(left) + 1
                self._cancel.wait(min(1.0, left))
        finally:
            self.resting = False
            self.rest_left = 0
            self._rested_seconds += time.time() - started_rest


class PullPhase(Phase):
    """Copy originals from the phone (or register local files) into the library."""

    kind = "pull"
    resume_state = "pending"

    def handle(self, meta):
        item_id = meta["id"]
        dest = library.original_path(item_id)
        library.update(item_id, state="pulling", error=None)

        if meta["source"] == "phone":
            with _CopyProgress(self, dest, meta["original_size"]):
                phone.pull(meta["source_path"], dest)
        else:
            # Folder mode: copy in so the original is preserved even if the
            # user moves or edits the source afterwards.
            copy_with_progress(meta["source_path"], dest, self)

        if not os.path.exists(dest):
            raise RuntimeError("pull produced no file")
        os.utime(dest, (meta["mtime"], meta["mtime"]))

        pr = probe(dest)
        thumbs.from_local(dest, library.thumb_path(item_id), pr.duration)
        library.update(item_id, state="pulled",
                       original_size=os.path.getsize(dest),
                       probe={"width": pr.width, "height": pr.height,
                              "duration": pr.duration, "fps": pr.fps,
                              "codec": pr.video_codec, "bit_rate": pr.bit_rate,
                              "is_hdr": pr.is_hdr})
        self._log(f"pulled {meta['name']}")


class EncodePhase(Phase):
    """Compress pulled items. Runs fully offline — no phone needed."""

    kind = "encode"
    resume_state = "pulled"

    # Rough seconds of video per byte, used only when a clip was never probed.
    _FALLBACK_BYTES_PER_SECOND = 6_000_000

    # Higher than a copy: two ffprobe runs, ffmpeg start-up and an exiftool pass
    # per item. Fitting wall time against duration over 1-16s clips gave
    # wall = 0.123 * duration + 1.93s on this machine.
    item_overhead_s = 2.0

    def _weight(self, meta):
        """Encoding cost tracks how long the video runs, not how big it is.

        A 60s 4K clip and a 60s 1080p clip differ in size several-fold but take
        far more similar times to encode, since the work is per frame. Duration
        comes from the probe done at pull time; the byte estimate is only a
        fallback for items that predate it.
        """
        duration = float((meta.get("probe") or {}).get("duration") or 0)
        if duration > 0:
            return duration
        return max(float(meta.get("original_size") or 0), 1.0) / self._FALLBACK_BYTES_PER_SECOND

    def handle(self, meta):
        item_id = meta["id"]
        src = library.original_path(item_id)
        if not os.path.exists(src):
            raise RuntimeError("original missing; pull it again")
        dst = library.compressed_path(item_id)
        library.update(item_id, state="encoding", error=None,
                       profile=self.opts.profile)

        pr = probe(src)
        enc = Encoder()
        with self._lock:
            self._encoder = enc
        try:
            enc.run(src, dst, self.opts, pr, self.log_path)
        finally:
            with self._lock:
                self._encoder = None

        metadata.copy_all_tags(src, dst)
        metadata.restore_file_times(src, dst)

        out_pr = probe(dst)
        if abs(out_pr.duration - pr.duration) > DURATION_TOLERANCE_S:
            raise RuntimeError(
                f"duration mismatch ({out_pr.duration:.1f}s vs {pr.duration:.1f}s)")

        out_size = os.path.getsize(dst)
        if out_size >= meta["original_size"]:
            self._log(f"kept original {meta['name']}: not smaller")
            library.update(item_id, state="kept_original", compressed_size=out_size)
            return

        library.update(item_id, state="ready_for_review", compressed_size=out_size)
        self._log(f"encoded {meta['name']}: "
                  f"{meta['original_size']} -> {out_size} bytes")


class PushPhase(Phase):
    """Write approved compressed files back. Only ever touches approved items."""

    kind = "push"
    resume_state = "approved"

    def __init__(self, item_ids, opts=None, output_dir=None):
        super().__init__(item_ids, opts)
        self.output_dir = output_dir

    def _weight(self, meta):
        """Pushing sends the compressed file, so that is what costs time."""
        return max(float(meta.get("compressed_size")
                         or meta.get("original_size") or 0), 1.0)

    def handle(self, meta):
        item_id = meta["id"]
        if meta["state"] != "approved":
            raise RuntimeError(f"refusing to push item in state '{meta['state']}'")
        src = library.compressed_path(item_id)
        if not os.path.exists(src):
            raise RuntimeError("compressed file missing")

        if meta["source"] == "phone":
            remote = phone.stat_remote(meta["source_path"])
            if not remote:
                raise RuntimeError("file no longer on phone")
            if remote[0] != meta["original_size"]:
                raise RuntimeError("file on phone changed since it was pulled")
            phone.push_replace(src, meta["source_path"], meta["mtime"])
            dest_desc = meta["source_path"]
        else:
            if not self.output_dir:
                raise RuntimeError("no output folder chosen")
            rel = meta.get("rel_path") or meta["name"]
            dest = os.path.join(self.output_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            copy_with_progress(src, dest, self)
            os.utime(dest, (meta["mtime"], meta["mtime"]))
            dest_desc = dest

        library.update(item_id, state="pushed")
        self._log(f"pushed {meta['name']} -> {dest_desc}")


def check_disk_space(total_bytes):
    """Library keeps originals AND compressed files, so require room for both."""
    ensure_data_dirs()
    free = shutil.disk_usage(LIBRARY_DIR).free
    needed = int(total_bytes * 1.25) + 2 * 1024**3
    if free < needed:
        raise RuntimeError(
            f"Not enough free disk space: need ~{needed // 1024**3}GB "
            f"(both originals and compressed files are kept), "
            f"have {free // 1024**3}GB")
