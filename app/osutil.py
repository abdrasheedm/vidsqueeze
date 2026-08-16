"""Small OS-specific helpers that differ per platform."""
import os
import subprocess

from .config import IS_MACOS, IS_WINDOWS, NO_WINDOW


def pid_alive(pid):
    """Is a process with this pid running?

    os.kill(pid, 0) is the POSIX idiom, but on Windows os.kill ignores the
    signal and calls TerminateProcess — using it as a liveness check would kill
    the very server we are checking for.
    """
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # "Invalid parameter" is what Windows returns for a pid that no longer
        # exists; anything else (typically access denied) means it does.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def open_in_file_manager(path):
    """Show a folder in Explorer / Finder / the desktop file manager."""
    path = os.path.abspath(path)
    if IS_WINDOWS:
        # os.startfile is the native way and needs no console process.
        os.startfile(path)  # noqa: S606
        return
    cmd = ["open", path] if IS_MACOS else ["xdg-open", path]
    subprocess.Popen(cmd, **NO_WINDOW)
