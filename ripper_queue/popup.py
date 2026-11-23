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


def enabled(conn=None) -> bool:
    if os.environ.get("RIPPER_POPUP", "1") == "0":
        return False
    if conn is not None and store.meta_get(conn, OFF_KEY) == "1":
        return False
    return True


def command(title: str, lead: str = "", sub: str = "") -> list:
    cmd = [sys.executable, str(SCRIPT), "--title", title]
    if lead:
        cmd += ["--lead", lead]
    if sub:
        cmd += ["--sub", sub]
    return cmd


def show(title: str, lead: str = "", sub: str = "", conn=None) -> bool:
    """Launch the pop-up; True when it was started. Never raises."""
    if not enabled(conn) or not title or sys.platform != "win32":
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(command(title, lead, sub), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        return True
    except OSError:
        return False
