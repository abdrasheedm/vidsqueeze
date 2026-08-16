"""FastAPI server for the local web UI."""
import mimetypes
import os
import re
import shutil
import subprocess

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import ledger, library, phone, thumbs
from .config import (LIBRARY_DIR, PIDFILE, VIDEO_EXTENSIONS, ensure_data_dirs)
from .encoder import EncodeOptions
from .job import EncodePhase, PullPhase, PushPhase, check_disk_space

app = FastAPI(title="VidSqueeze")
_phase = None            # the single running phase, if any


@app.on_event("startup")
def _startup():
    ensure_data_dirs()
    other = _other_server_pid()
    if other:
        print(f"[startup] another VidSqueeze server is running (pid {other}); "
              f"not touching shared state")
        return
    _write_pidfile()
    # Roll interrupted items back to a resumable state. Never deletes finished work.
    library.recover_interrupted()


def _other_server_pid():
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _write_pidfile():
    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


# ---------------------------------------------------------------- device / picker

@app.get("/api/device")
def device():
    status = phone.device_status()
    usage = library.disk_usage()
    disk = shutil.disk_usage(os.path.expanduser("~"))
    status.update({
        "library_bytes": usage["total"],
        "library_originals": usage["originals"],
        "library_compressed": usage["compressed"],
        "library_dir": LIBRARY_DIR,
        "mac_free_bytes": disk.free,
    })
    return status


@app.get("/api/videos")
def videos():
    try:
        state = phone.device_status()["state"]
        if state != "connected":
            raise phone.PhoneError(
                "No phone connected" if state in ("none", "no-adb")
                else "Phone connected but not authorized — tap Allow on the phone")
        items = phone.list_videos()
    except phone.PhoneError as e:
        raise HTTPException(status_code=503, detail=str(e))
    known = library.source_paths_in_library()
    for v in items:
        m = known.get(v["path"])
        v["item_id"] = m["id"] if m else None
        v["item_state"] = m["state"] if m else None
        v["thumb_key"] = thumbs.cache_key(v["path"], v["mtime"], v["size"])
    return {"videos": items}


class FolderScanRequest(BaseModel):
    path: str


@app.post("/api/folder/scan")
def folder_scan(req: FolderScanRequest):
    root = os.path.expanduser(req.path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=400, detail=f"Not a folder: {root}")
    known = library.source_paths_in_library()
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(VIDEO_EXTENSIONS):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            m = known.get(full)
            out.append({
                "path": full, "name": name, "folder": dirpath,
                "rel_path": os.path.relpath(full, root),
                "size": st.st_size, "mtime": int(st.st_mtime),
                "item_id": m["id"] if m else None,
                "item_state": m["state"] if m else None,
                "thumb_key": None,
            })
    out.sort(key=lambda v: (v["folder"], -v["mtime"]))
    return {"videos": out, "root": root}


@app.get("/api/thumb")
def thumb(path: str, mtime: int, size: int):
    """Poster frame for a phone file, extracted from its first megabytes."""
    p = thumbs.from_phone(path, mtime, size)
    if not p:
        raise HTTPException(status_code=404, detail="no thumbnail")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/items/{item_id}/thumb")
def item_thumb(item_id: str):
    p = library.thumb_path(item_id)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="no thumbnail")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- media streaming

def _ranged_file(path, request: Request):
    """Serve a file with HTTP Range support so <video> can seek."""
    size = os.path.getsize(path)
    media_type = mimetypes.guess_type(path)[0] or "video/mp4"
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type,
                            headers={"Accept-Ranges": "bytes"})

    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        raise HTTPException(status_code=416, detail="bad range")
    start = int(m.group(1)) if m.group(1) else 0
    end = int(m.group(2)) if m.group(2) else size - 1
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(stream(), status_code=206, media_type=media_type,
                             headers={
                                 "Content-Range": f"bytes {start}-{end}/{size}",
                                 "Accept-Ranges": "bytes",
                                 "Content-Length": str(length),
                             })


@app.get("/api/media/{item_id}/{which}")
def media(item_id: str, which: str, request: Request):
    if which not in ("original", "compressed"):
        raise HTTPException(status_code=400, detail="which must be original|compressed")
    path = (library.original_path(item_id) if which == "original"
            else library.compressed_path(item_id))
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"{which} not available")
    return _ranged_file(path, request)


# ---------------------------------------------------------------- library / review

@app.get("/api/library")
def get_library():
    items = library.all_items()
    counts = {}
    for m in items:
        counts[m["state"]] = counts.get(m["state"], 0) + 1
    return {"items": items, "counts": counts, "usage": library.disk_usage()}


class ApproveRequest(BaseModel):
    item_ids: list[str]
    approved: bool = True


@app.post("/api/items/approve")
def approve(req: ApproveRequest):
    changed = []
    for item_id in req.item_ids:
        meta = library.load(item_id)
        if not meta:
            continue
        # Only a reviewable item can be approved; never flip an already-pushed one.
        if req.approved and meta["state"] in ("ready_for_review", "rejected"):
            changed.append(library.update(item_id, state="approved"))
        elif not req.approved and meta["state"] in ("ready_for_review", "approved"):
            changed.append(library.update(item_id, state="rejected"))
    return {"updated": len(changed)}


class DeleteRequest(BaseModel):
    item_ids: list[str] = []
    originals_of_pushed: bool = False


@app.post("/api/library/cleanup")
def cleanup(req: DeleteRequest):
    freed = 0
    if req.originals_of_pushed:
        freed += library.delete_originals(states=("pushed",))
    for item_id in req.item_ids:
        library.delete_item(item_id)
    return {"freed": freed, "usage": library.disk_usage()}


# ---------------------------------------------------------------- phases

class PullRequest(BaseModel):
    videos: list[dict]           # {path,name,folder,size,mtime,rel_path?}
    source: str = "phone"        # phone | folder


@app.post("/api/phases/pull")
def start_pull(req: PullRequest):
    global _phase
    _require_idle()
    if not req.videos:
        raise HTTPException(status_code=400, detail="No videos selected")
    check_disk_space(sum(v["size"] for v in req.videos))

    item_ids = []
    for v in req.videos:
        meta = library.create(req.source, v["path"], v["name"], v["size"],
                              v["mtime"], v.get("folder", ""))
        if v.get("rel_path"):
            library.update(meta["id"], rel_path=v["rel_path"])
        item_ids.append(meta["id"])
    _phase = PullPhase(item_ids).start()
    return _phase.snapshot()


class EncodeRequest(BaseModel):
    item_ids: list[str] = []     # empty = every pulled item
    profile: str = "quality"
    crf: int = 32
    preset: str = "slow"
    vt_quality: int = 55
    max_long_edge: int = 1920
    audio_bitrate: str = "128k"


@app.post("/api/phases/encode")
def start_encode(req: EncodeRequest):
    global _phase
    _require_idle()
    ids = req.item_ids or [m["id"] for m in library.items_in_state("pulled")]
    if not ids:
        raise HTTPException(status_code=400, detail="Nothing to encode")
    if req.profile not in ("quality", "fast"):
        raise HTTPException(status_code=400, detail="Unknown profile")
    opts = EncodeOptions(
        profile=req.profile,
        crf=max(18, min(40, req.crf)),
        preset=req.preset if req.preset in ("medium", "slow") else "slow",
        vt_quality=max(1, min(100, req.vt_quality)),
        max_long_edge=req.max_long_edge if req.max_long_edge > 0 else 0,
        audio_bitrate=req.audio_bitrate,
    )
    _phase = EncodePhase(ids, opts).start()
    return _phase.snapshot()


class PushRequest(BaseModel):
    item_ids: list[str] = []     # empty = every approved item
    output_dir: str = ""


@app.post("/api/phases/push")
def start_push(req: PushRequest):
    global _phase
    _require_idle()
    ids = req.item_ids or [m["id"] for m in library.items_in_state("approved")]
    if not ids:
        raise HTTPException(status_code=400, detail="Nothing approved to push")
    metas = [library.load(i) for i in ids]
    if any(m and m["state"] != "approved" for m in metas):
        raise HTTPException(status_code=400,
                            detail="Only approved items can be pushed")
    if any(m and m["source"] == "phone" for m in metas):
        if phone.device_status()["state"] != "connected":
            raise HTTPException(status_code=503,
                                detail="Connect the phone to push these items")
    out_dir = os.path.expanduser(req.output_dir) if req.output_dir else None
    _phase = PushPhase(ids, output_dir=out_dir).start()
    return _phase.snapshot()


def _require_idle():
    if _phase and _phase.state == "running":
        raise HTTPException(status_code=409,
                            detail=f"A {_phase.kind} phase is already running")


@app.get("/api/phases/current")
def current_phase():
    if _phase is None:
        return {"state": "idle"}
    snap = _phase.snapshot()
    if snap["current"]:
        meta = library.load(snap["current"])
        snap["current_item"] = meta
    return snap


@app.post("/api/phases/cancel")
def cancel_phase():
    if _phase is None or _phase.state != "running":
        raise HTTPException(status_code=400, detail="No running phase")
    _phase.cancel()
    return {"ok": True}


@app.get("/api/history")
def history():
    return {"runs": ledger.runs()}


@app.post("/api/open-library")
def open_library():
    subprocess.Popen(["open", LIBRARY_DIR])
    return {"ok": True}
