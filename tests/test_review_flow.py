"""Review/approve workflow test with a faked phone (a local directory).

The critical assertion: the phone file is NOT modified until the item is
explicitly approved and a push phase runs.

Run:  .venv/bin/python tests/test_review_flow.py   (Windows: .venv\\Scripts\\python)
"""
import os
import platform
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import hwaccel, library, phone  # noqa: E402
from app.config import NO_WINDOW, FFMPEG, LIBRARY_DIR  # noqa: E402
from app.encoder import EncodeOptions  # noqa: E402
from app.job import EncodePhase, PullPhase, PushPhase  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_review")
FAKE = os.path.join(TMP, "fakephone")
OUTDIR = os.path.join(TMP, "folder_out")
MTIME = 1717236000
failures = []


def check(name, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def wait(phase, limit=180):
    for _ in range(limit * 2):
        if phase.state != "running":
            return
        time.sleep(0.5)
    raise RuntimeError(f"{phase.kind} phase did not finish")


def make_video(path, seconds=2, size="1280x720"):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", str(seconds), "-shortest",
                    "-metadata", "creation_time=2024-06-01T10:00:00.000000Z",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-b:v", "6M", "-c:a", "aac", path], check=True, **NO_WINDOW)
    os.utime(path, (MTIME, MTIME))


# ---- fake ADB layer -------------------------------------------------------
def fake_pull(remote, local, timeout=0):
    shutil.copy2(os.path.join(FAKE, os.path.basename(remote)), local)


def fake_stat_remote(remote):
    p = os.path.join(FAKE, os.path.basename(remote))
    if not os.path.exists(p):
        return None
    st = os.stat(p)
    return st.st_size, int(st.st_mtime)


def fake_push_replace(local, remote, mtime, timeout=0):
    dst = os.path.join(FAKE, os.path.basename(remote))
    shutil.copy(local, dst)
    os.utime(dst, (mtime, mtime))


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(FAKE); os.makedirs(OUTDIR)
    phone.pull, phone.stat_remote, phone.push_replace = (
        fake_pull, fake_stat_remote, fake_push_replace)
    print(f"platform: {platform.system()} {platform.release()} "
          f"({platform.machine()})")
    print(f"fast profile: {hwaccel.summary()['detail']}")
    print()

    created = []

    print("== Phone mode: pull -> encode -> review gate -> approve -> push ==")
    name = "20240601_100000.mp4"
    src = os.path.join(FAKE, name)
    make_video(src)
    orig_size = os.path.getsize(src)
    remote_path = f"/sdcard/DCIM/Camera/{name}"

    meta = library.create("phone", remote_path, name, orig_size, MTIME,
                          "/sdcard/DCIM/Camera")
    created.append(meta["id"])
    item_id = meta["id"]

    p = PullPhase([item_id]).start(); wait(p)
    meta = library.load(item_id)
    check("after pull: state is 'pulled'", meta["state"] == "pulled", meta["state"])
    check("after pull: original stored in library",
          os.path.exists(library.original_path(item_id)))
    check("after pull: thumbnail generated",
          os.path.exists(library.thumb_path(item_id)))
    check("after pull: probe recorded", bool(meta.get("probe")))

    e = EncodePhase([item_id], EncodeOptions(profile="fast", hw_quality=40,
                                             max_long_edge=640)).start()
    wait(e)
    meta = library.load(item_id)
    check("after encode: state is 'ready_for_review'",
          meta["state"] == "ready_for_review", f"{meta['state']} err={meta.get('error')}")
    check("after encode: compressed file exists",
          os.path.exists(library.compressed_path(item_id)))
    check("after encode: original still kept (nothing auto-deleted)",
          os.path.exists(library.original_path(item_id)))
    check("after encode: PHONE FILE UNTOUCHED",
          os.path.getsize(src) == orig_size,
          f"{os.path.getsize(src)} vs {orig_size}")

    # Push must refuse an unapproved item.
    bad = PushPhase([item_id]).start(); wait(bad)
    check("push refuses unapproved item", library.load(item_id)["state"] == "failed",
          library.load(item_id)["state"])
    check("phone still untouched after refused push",
          os.path.getsize(src) == orig_size)

    library.update(item_id, state="ready_for_review", error=None)
    library.update(item_id, state="approved")
    push = PushPhase([item_id]).start(); wait(push)
    meta = library.load(item_id)
    check("after approve+push: state is 'pushed'", meta["state"] == "pushed",
          f"{meta['state']} err={meta.get('error')}")
    check("phone file now smaller", os.path.getsize(src) < orig_size,
          f"{os.path.getsize(src)} vs {orig_size}")
    check("phone mtime preserved", int(os.path.getmtime(src)) == MTIME)
    check("both copies still kept locally",
          os.path.exists(library.original_path(item_id))
          and os.path.exists(library.compressed_path(item_id)))

    print("== Resume: interrupted states roll back to resumable ones ==")
    r1 = library.create("phone", "/sdcard/DCIM/Camera/x1.mp4", "x1.mp4", 10, MTIME, "/sdcard/DCIM/Camera")
    r2 = library.create("phone", "/sdcard/DCIM/Camera/x2.mp4", "x2.mp4", 10, MTIME, "/sdcard/DCIM/Camera")
    created += [r1["id"], r2["id"]]
    library.update(r1["id"], state="pulling")
    library.update(r2["id"], state="encoding")
    library.recover_interrupted()
    check("interrupted pull -> pending", library.load(r1["id"])["state"] == "pending")
    check("interrupted encode -> pulled", library.load(r2["id"])["state"] == "pulled")

    print("== Folder mode: source file read, output written on approve ==")
    fsrc_dir = os.path.join(TMP, "folder_in")
    os.makedirs(fsrc_dir)
    fname = "clip.mp4"
    fsrc = os.path.join(fsrc_dir, fname)
    make_video(fsrc)
    fmeta = library.create("folder", fsrc, fname, os.path.getsize(fsrc), MTIME, fsrc_dir)
    created.append(fmeta["id"])
    library.update(fmeta["id"], rel_path=fname)
    fid = fmeta["id"]

    wait(PullPhase([fid]).start())
    wait(EncodePhase([fid], EncodeOptions(profile="fast", hw_quality=40,
                                          max_long_edge=640)).start())
    check("folder mode: ready for review",
          library.load(fid)["state"] == "ready_for_review",
          library.load(fid)["state"])
    check("folder mode: source file untouched",
          os.path.getsize(fsrc) == fmeta["original_size"])
    library.update(fid, state="approved")
    wait(PushPhase([fid], output_dir=OUTDIR).start())
    out_file = os.path.join(OUTDIR, fname)
    check("folder mode: output written", os.path.exists(out_file))
    check("folder mode: output smaller",
          os.path.exists(out_file) and os.path.getsize(out_file) < os.path.getsize(fsrc))
    check("folder mode: source still intact",
          os.path.getsize(fsrc) == fmeta["original_size"])

    print("== Disk usage accounting ==")
    usage = library.disk_usage()
    check("usage reports originals and compressed",
          usage["originals"] > 0 and usage["compressed"] > 0, str(usage))

    for iid in created:
        library.delete_item(iid)
    shutil.rmtree(TMP, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("Review flow test passed ✅")


if __name__ == "__main__":
    main()
