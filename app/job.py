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


class Phase:
    """Base for a background phase operating over library items."""

    kind = "phase"

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

    def _log(self, msg):
        with open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    def _run(self):
        failures = 0
        try:
            for item_id in self.item_ids:
                if self._cancel.is_set():
                    break
                meta = library.load(item_id)
                if meta is None:
                    continue
                self.current = item_id
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
            "progress": (self._encoder.progress if self._encoder else 0.0),
            "speed": (self._encoder.speed if self._encoder else ""),
        }


class PullPhase(Phase):
    """Copy originals from the phone (or register local files) into the library."""

    kind = "pull"
    resume_state = "pending"

    def handle(self, meta):
        item_id = meta["id"]
        dest = library.original_path(item_id)
        library.update(item_id, state="pulling", error=None)

        if meta["source"] == "phone":
            phone.pull(meta["source_path"], dest)
        else:
            # Folder mode: copy in so the original is preserved even if the user
            # moves or edits the source afterwards.
            shutil.copy2(meta["source_path"], dest)

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
            shutil.copy2(src, dest)
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
