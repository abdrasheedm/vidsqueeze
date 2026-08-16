"""ADB wrapper for the phone side: detect, list, pull, push-and-replace, rescan."""
import datetime
import os
import shlex
import subprocess

from .config import ADB, PHONE_VIDEO_DIRS, VIDEO_EXTENSIONS


class PhoneError(Exception):
    pass


def _adb(*args, timeout=30):
    try:
        out = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PhoneError("adb is not installed (run setup.sh)")
    except subprocess.TimeoutExpired:
        raise PhoneError(f"adb timed out: {' '.join(args[:3])}...")
    return out


def _adb_check(*args, timeout=30):
    out = _adb(*args, timeout=timeout)
    if out.returncode != 0:
        raise PhoneError((out.stderr.strip() or out.stdout.strip())[:500])
    return out.stdout


def _shell(command, timeout=30):
    return _adb_check("shell", command, timeout=timeout)


def device_status():
    """Returns {state, model, free_bytes} — state: none|unauthorized|connected|no-adb."""
    out = _adb("devices")
    if out.returncode != 0:
        return {"state": "no-adb", "model": None, "free_bytes": None}
    lines = [l for l in out.stdout.strip().splitlines()[1:] if l.strip()]
    if not lines:
        return {"state": "none", "model": None, "free_bytes": None}
    state = lines[0].split()[-1]
    if state == "unauthorized":
        return {"state": "unauthorized", "model": None, "free_bytes": None}
    if state != "device":
        return {"state": "none", "model": None, "free_bytes": None}
    try:
        model = _shell("getprop ro.product.model").strip()
        df = _shell("df -k /sdcard").strip().splitlines()
        free_bytes = int(df[-1].split()[3]) * 1024 if len(df) > 1 else None
    except (PhoneError, ValueError, IndexError):
        model, free_bytes = None, None
    return {"state": "connected", "model": model or "Android device", "free_bytes": free_bytes}


def list_videos():
    """List camera videos: [{path, name, size, mtime}] sorted newest first."""
    videos = []
    conds = " -o ".join(f"-iname '*{ext}'" for ext in VIDEO_EXTENSIONS)
    seen = set()
    for base in PHONE_VIDEO_DIRS:
        cmd = (f"find {shlex.quote(base)} -type f \\( {conds} \\) "
               f"-exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null")
        out = _adb("shell", cmd, timeout=120)
        for line in out.stdout.splitlines():
            size, _, rest = line.partition("|")
            mtime, _, path = rest.partition("|")
            if not (size.isdigit() and mtime.lstrip("-").isdigit() and path):
                continue
            if path in seen:
                continue
            seen.add(path)
            videos.append({
                "path": path,
                "name": os.path.basename(path),
                "folder": os.path.dirname(path),
                "size": int(size),
                "mtime": int(mtime),
            })
    videos.sort(key=lambda v: (v["folder"], -v["mtime"]))
    return videos


def stat_remote(path):
    """(size, mtime) of a remote file, or None if missing."""
    out = _adb("shell", f"stat -c '%s|%Y' {shlex.quote(path)}")
    parts = out.stdout.strip().split("|")
    if out.returncode != 0 or len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def pull(remote, local, timeout=1800):
    _adb_check("pull", "-a", remote, local, timeout=timeout)
    if not os.path.exists(local):
        raise PhoneError(f"pull produced no file: {remote}")


def _set_remote_mtime(remote, mtime):
    q = shlex.quote(remote)
    attempts = [f"touch -m -d @{mtime} {q}"]
    # Fallback: -t in the phone's local timezone
    try:
        offset = _shell("date +%z").strip()  # e.g. +0530
        sign = 1 if offset.startswith("+") else -1
        secs = sign * (int(offset[1:3]) * 3600 + int(offset[3:5]) * 60)
        local_ts = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc) \
            + datetime.timedelta(seconds=secs)
        attempts.append(f"touch -m -t {local_ts.strftime('%Y%m%d%H%M.%S')} {q}")
    except (PhoneError, ValueError):
        pass
    for cmd in attempts:
        _adb("shell", cmd)
        st = stat_remote(remote)
        if st and abs(st[1] - mtime) <= 1:
            return True
    return False


def media_rescan(remote):
    """Ask MediaProvider to rescan the file; method varies by Android version."""
    q = shlex.quote(remote)
    results = []
    for cmd in (
        f"content call --uri content://media/external/file --method scan_file --arg {q}",
        f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{q}",
        "content call --method scan_volume --uri content://media --arg external_primary",
    ):
        out = _adb("shell", cmd, timeout=60)
        text = (out.stdout + out.stderr).lower()
        ok = out.returncode == 0 and "exception" not in text and "error" not in text
        results.append(ok)
        if ok and "scan_volume" not in cmd:
            break
    return any(results)


def push_replace(local, remote, mtime, timeout=1800):
    """Push compressed file next to the original, verify, swap it in, fix mtime."""
    tmp = remote + ".vstmp"
    q_tmp, q_remote = shlex.quote(tmp), shlex.quote(remote)
    try:
        _adb_check("push", local, tmp, timeout=timeout)
        pushed = stat_remote(tmp)
        if not pushed or pushed[0] != os.path.getsize(local):
            raise PhoneError(f"pushed size mismatch for {remote}")
        _shell(f"mv {q_tmp} {q_remote}")
    except PhoneError:
        _adb("shell", f"rm -f {q_tmp}")
        raise
    _set_remote_mtime(remote, mtime)
    media_rescan(remote)
