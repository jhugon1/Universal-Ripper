"""Starts the Ripper whenever Traktor opens. Run at Windows start-up by a shortcut that Setup creates (see autostart.ps1); it has no
window and uses almost nothing: once every couple of seconds it looks at the process list.

  - Traktor opens (or is already open when you sign in)  ->  `run.py --background` is started: the server, the RIPPER button and
    the pop-up, but not the big window (the RIPPER button opens that when you want it).
  - A Ripper that is already running (you started it yourself) is left alone.
  - If you quit the Ripper while Traktor stays open, it stays quit: it is only started once per Traktor session.
  - The Ripper that was started this way quits by itself a little after Traktor closes (and never in the middle of a download).
"""
import ctypes
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 2.0


def log(text):
    """A tiny log (%LOCALAPPDATA%\\RippedRipper\\watcher.log, trimmed when it grows)."""
    try:
        folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RippedRipper"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "watcher.log"
        if path.exists() and path.stat().st_size > 100_000:
            path.unlink()
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


class Watcher:
    """The decision logic, with the outside world passed in so it can be tested without Traktor."""

    def __init__(self, traktor_pids, ripper_running, start_ripper, say=log):
        self.traktor_pids, self.ripper_running, self.start_ripper, self.say = traktor_pids, ripper_running, start_ripper, say
        self.handled = set()   # Traktor process ids that already had their chance to start the Ripper

    def poll(self):
        """One look. Returns 'started', 'already-running', 'waiting' or 'idle'."""
        pids = set(self.traktor_pids())
        self.handled &= pids   # a Traktor that closed is forgotten: its next start is a new session
        fresh = pids - self.handled
        if not fresh:
            return "waiting" if pids else "idle"
        self.handled |= pids
        if self.ripper_running():
            self.say(f"Traktor {sorted(fresh)} is open; the Ripper is already running, leaving it alone")
            return "already-running"
        self.say(f"Traktor {sorted(fresh)} is open: starting the Ripper in the background")
        self.start_ripper()
        return "started"


def ripper_running():
    """True when a Ripper already answers on its port (GET /api/popup is the app's own)."""
    port = int(os.environ.get("RIPPER_PORT") or 8000)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/popup", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def start_ripper_in_background():
    python = Path(sys.executable).with_name("python.exe")   # pythonw.exe has no console streams; a hidden console works for the server
    python = python if python.is_file() else Path(sys.executable)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([str(python), str(ROOT / "run.py"), "--background"], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def only_one_instance():
    """True for the first watcher; a second one (for example Setup starting it again) sees the name taken and just exits."""
    ctypes.windll.kernel32.SetLastError(0)
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\RippedRipperTraktorWatcher")
    return bool(handle) and ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def main():
    if sys.platform != "win32":
        return 0
    if not only_one_instance():
        return 0
    sys.path.insert(0, str(ROOT))
    import traktor_process
    watcher = Watcher(traktor_process.traktor_pids, ripper_running, start_ripper_in_background)
    log("watcher started")
    while True:
        try:
            watcher.poll()
        except Exception as exc:   # never die: a dead watcher is an app that never starts
            log(f"error: {exc!r}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
