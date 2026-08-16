"""JSON ledger of processed files and past runs."""
import json
import os
import threading
import time

from .config import LEDGER_PATH

_lock = threading.Lock()


def _load():
    if not os.path.exists(LEDGER_PATH):
        return {"files": {}, "runs": []}
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"files": {}, "runs": []}


def _save(data):
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, LEDGER_PATH)


def processed_files():
    with _lock:
        return dict(_load()["files"])


def record_file(path, run_id, status, original_size, compressed_size):
    with _lock:
        data = _load()
        data["files"][path] = {
            "status": status,  # replaced | kept_original
            "original_size": original_size,
            "compressed_size": compressed_size,
            "run_id": run_id,
            "completed_at": int(time.time()),
        }
        _save(data)


def record_run(run):
    """Insert or update a run summary dict (keyed by run['id'])."""
    with _lock:
        data = _load()
        data["runs"] = [r for r in data["runs"] if r["id"] != run["id"]]
        data["runs"].append(run)
        data["runs"].sort(key=lambda r: r.get("started", 0), reverse=True)
        _save(data)


def runs():
    with _lock:
        return list(_load()["runs"])
