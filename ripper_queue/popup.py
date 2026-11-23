"""Start the "RIPPED" pop-up (popup_window.py) over Traktor when a rip succeeds.

It is a separate process so a problem there can never affect the app, and it is skipped when RIPPER_POPUP=0 or the
DJ switched it off on the page (saved in the database).
"""
import os
import subprocess
import sys
from pathlib import Path

from . import store

SCRIPT = Path(__file__).resolve().parent.parent / "popup_window.py"
OFF_KEY = "popup_off"


def gui_python() -> str:
    """The interpreter to start windows with. On Windows that is pythonw.exe (next to python.exe), which never opens a console.
    A venv's python.exe is only a launcher that starts the real interpreter as a child; started without a console of its own,
    that child makes Windows open a new terminal window for a moment, which is what this avoids."""
    exe = Path(sys.executable)
    if sys.platform == "win32":
        windowless = exe.with_name("pythonw.exe")
        if windowless.is_file():
            return str(windowless)
    return str(exe)


def enabled(conn=None) -> bool:
    if os.environ.get("RIPPER_POPUP", "1") == "0":
        return False
    if conn is not None and store.meta_get(conn, OFF_KEY) == "1":
        return False
    return True


def command(title: str, lead: str = "", sub: str = "", cover: str = "") -> list:
    cmd = [gui_python(), str(SCRIPT), "--title", title]
    if lead:
        cmd += ["--lead", lead]
    if sub:
        cmd += ["--sub", sub]
    if cover:
        cmd += ["--cover", cover]
    return cmd


def show(title: str, lead: str = "", sub: str = "", conn=None, cover: str = "") -> bool:
    """Launch the pop-up; True when it was started. Never raises."""
    if not enabled(conn) or not title or sys.platform != "win32":
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # not DETACHED_PROCESS: that is what made the launcher open a terminal
        subprocess.Popen(command(title, lead, sub, cover), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        return True
    except OSError:
        return False
