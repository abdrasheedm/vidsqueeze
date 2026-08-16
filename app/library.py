"""Durable item store: one folder per video, holding both files and its state.

Nothing here is ever auto-deleted — the user cleans up manually. State lives in
each item's meta.json, so the queue survives restarts and unplugged phones.
"""
import hashlib
import json
import os
import shutil
import threading
import time

from .config import LIBRARY_DIR, ensure_data_dirs

_lock = threading.RLock()

# pending -> pulling -> pulled -> encoding -> ready_for_review -> approved -> pushed
# side states: rejected, failed, kept_original (output not smaller)
ACTIVE_STATES = ("pending", "pulling", "pulled", "encoding")
REVIEW_STATES = ("ready_for_review",)
DONE_STATES = ("pushed", "rejected", "kept_original")


def make_item_id(source_path):
    h = hashlib.sha1(source_path.encode()).hexdigest()[:8]
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{h}"


def item_dir(item_id):
    return os.path.join(LIBRARY_DIR, item_id)


def original_path(item_id):
    return os.path.join(item_dir(item_id), "original.mp4")


def compressed_path(item_id):
    return os.path.join(item_dir(item_id), "compressed.mp4")


def thumb_path(item_id):
    return os.path.join(item_dir(item_id), "thumb.jpg")


def meta_path(item_id):
    return os.path.join(item_dir(item_id), "meta.json")


def create(source, source_path, name, size, mtime, folder, batch_id=None):
    """Create a new pending item and return its meta dict."""
    ensure_data_dirs()
    item_id = make_item_id(source_path)
    os.makedirs(item_dir(item_id), exist_ok=True)
    meta = {
        "id": item_id,
        "source": source,            # "phone" | "folder"
        "source_path": source_path,
        "name": name,
        "folder": folder,
        "original_size": size,
        "mtime": mtime,
        "state": "pending",
        "compressed_size": None,
        "error": None,
        "batch_id": batch_id,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "profile": None,
        "probe": None,
    }
    save(meta)
    return meta


def save(meta):
    with _lock:
        meta["updated_at"] = int(time.time())
        p = meta_path(meta["id"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f, indent=1)
        os.replace(tmp, p)


def load(item_id):
    try:
        with open(meta_path(item_id)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def update(item_id, **fields):
    with _lock:
        meta = load(item_id)
        if meta is None:
            return None
        meta.update(fields)
        save(meta)
        return meta


def all_items():
    """Every item on disk, newest first. This is the source of truth on restart."""
    ensure_data_dirs()
    items = []
    for name in os.listdir(LIBRARY_DIR):
        meta = load(name)
        if meta:
            items.append(meta)
    items.sort(key=lambda m: m.get("created_at", 0), reverse=True)
    return items


def items_in_state(*states):
    return [m for m in all_items() if m.get("state") in states]


def source_paths_in_library():
    """Map source_path -> latest item, so the picker can flag known videos."""
    out = {}
    for m in sorted(all_items(), key=lambda m: m.get("created_at", 0)):
        out[m["source_path"]] = m
    return out


def recover_interrupted():
    """On startup, roll back states that imply a live process back to a resumable one."""
    for m in all_items():
        st = m.get("state")
        if st == "pulling":
            # partial pull; drop the fragment and re-queue
            _remove_file(original_path(m["id"]))
            update(m["id"], state="pending", error=None)
        elif st == "encoding":
            _remove_file(compressed_path(m["id"]))
            update(m["id"], state="pulled", error=None)


def _remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def disk_usage():
    """Bytes used by originals vs compressed files across the library."""
    originals = compressed = 0
    for m in all_items():
        for path, which in ((original_path(m["id"]), "o"), (compressed_path(m["id"]), "c")):
            try:
                sz = os.path.getsize(path)
            except OSError:
                continue
            if which == "o":
                originals += sz
            else:
                compressed += sz
    return {"originals": originals, "compressed": compressed,
            "total": originals + compressed}


def delete_originals(states=("pushed",)):
    """Manual cleanup: drop originals for items in the given states. Returns bytes freed."""
    freed = 0
    for m in items_in_state(*states):
        p = original_path(m["id"])
        try:
            freed += os.path.getsize(p)
            os.remove(p)
        except OSError:
            pass
    return freed


def delete_item(item_id):
    """Remove an item and both its files entirely."""
    shutil.rmtree(item_dir(item_id), ignore_errors=True)


MEDIA_FILES = ("original.mp4", "compressed.mp4")


def purge_media(item_ids=None):
    """Delete the video files but keep each item's record and thumbnail.

    meta.json holds the sizes the "space saved" figure is computed from, and the
    thumbnail is what makes an entry recognisable afterwards. Removing the whole
    item directory would erase the record of everything that was ever
    compressed, so only the two large files go.
    """
    items = ([m for m in (load(i) for i in item_ids) if m]
             if item_ids is not None else all_items())
    freed = purged = 0
    for m in items:
        gone = 0
        for name in MEDIA_FILES:
            p = os.path.join(item_dir(m["id"]), name)
            try:
                size = os.path.getsize(p)
                os.remove(p)
                gone += size
            except OSError:
                pass
        if gone:
            purged += 1
            freed += gone
        update(m["id"], files_deleted=True)
    return {"purged": purged, "freed": freed, "records_kept": len(items)}


def saved_bytes():
    """Total space saved, from the records rather than the files on disk.

    Deliberately independent of whether the videos still exist, so the figure
    survives a cleanup.
    """
    return sum(max(0, (m.get("original_size") or 0) - (m.get("compressed_size") or 0))
               for m in all_items()
               if m.get("compressed_size")
               and m.get("state") in ("ready_for_review", "approved", "pushed"))


def cleanup_preview():
    """What each cleanup action would free, without removing anything."""
    originals_of_pushed = 0
    for m in items_in_state("pushed"):
        try:
            originals_of_pushed += os.path.getsize(original_path(m["id"]))
        except OSError:
            pass
    usage = disk_usage()
    items = all_items()
    with_files = sum(1 for m in items
                     if os.path.exists(original_path(m["id"]))
                     or os.path.exists(compressed_path(m["id"])))
    return {
        "originals_of_pushed": originals_of_pushed,
        "everything": usage["total"],
        "items": len(items),
        "with_files": with_files,
        "unpushed": sum(1 for m in items if m.get("state") not in DONE_STATES),
        "saved_bytes": saved_bytes(),
    }
