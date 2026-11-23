"""Cross-platform launcher: web app on 127.0.0.1:8000 (this machine only), then open the page.

  python run.py             start everything and open the page in an app window (Chrome or Edge)
  python run.py --dev       the same with a separate dev database and folders, and the page reloads
                            when .py, template or static files change
  python run.py --no-window start the server only (open http://127.0.0.1:8000/ yourself)
"""
import argparse
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

PORT = 8000
APP_URL = f"http://127.0.0.1:{PORT}/"  # 127.0.0.1, not localhost: the Spotify login returns to this exact address
ROOT = Path(__file__).resolve().parent


def current_branch(root=ROOT):
    """Name of the checked-out git branch, or None (not a git checkout, or git is not installed)."""
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def queue_code_present(root=ROOT) -> bool:
    return all((Path(root) / "ripper_queue" / name).is_file() for name in ("store.py", "worker.py", "files.py"))


def checkout_report(root=ROOT) -> list:
    """Lines to print at startup: the branch, and a warning if this checkout has no queue code
    (then 'Add to queue' and the Tonight's Queue panel do not exist)."""
    branch = current_branch(root)
    lines = [f"Branch: {branch or 'unknown (not a git checkout?)'}"]
    if not queue_code_present(root):
        lines.append("WARNING: the queue code (ripper_queue/) is not in this checkout, so 'Add to queue' and the "
                     f"Tonight's Queue panel will not work. You are on branch {branch or 'unknown'}; check out the "
                     "branch that contains the queue (see `git branch --list`).")
    return lines


def dev_defaults(environ=None, root=ROOT) -> dict:
    """In dev mode use a separate database and queue folder, unless the caller set their own.
    Fake or experimental runs must never write into the real queue.db or the real Ripper Queue
    folder that Traktor browses. Returns what was set."""
    environ = os.environ if environ is None else environ
    dev_dir = Path(root) / "dev_data"
    chosen = {}
    for name, value in (("RIPPER_DB", dev_dir / "queue.dev.db"), ("RIPPER_QUEUE_DIR", dev_dir / "Ripper Queue"),
                        ("RIPPER_LIBRARY_DIR", dev_dir / "Library"), ("RIPPER_PLAYLIST_DIR", dev_dir / "Playlist Rips")):
        if not environ.get(name):
            environ[name] = chosen[name] = str(value)
    if chosen:
        dev_dir.mkdir(parents=True, exist_ok=True)
    return chosen


def uvicorn_kwargs(dev: bool) -> dict:
    kwargs = {"host": "127.0.0.1", "port": PORT}
    if dev:
        # watchfiles (requirements-dev.txt) skips .venv; uvicorn's fallback reloader would scan it.
        kwargs.update(
            reload=True,
            reload_dirs=[str(ROOT)],
            reload_includes=["*.py"],
            reload_excludes=["tests/*", "tools/*", "fixtures/*"],
        )
    return kwargs


def find_chromium():
    """Path of an installed Chrome or Edge (Chrome first), or None."""
    if sys.platform.startswith("win"):
        roots = [r for r in (os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")) if r]
        for rel in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            for root in roots:
                if (Path(root) / rel).is_file():
                    return str(Path(root) / rel)
        return None
    if sys.platform == "darwin":
        for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
            if Path(app).is_file():
                return app
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def window_command(url, exe=None):
    """Command line for a chromeless app window, or None if no Chrome/Edge is installed."""
    exe = exe or find_chromium()
    return [exe, f"--app={url}", "--window-size=1440,900"] if exe else None


def open_page(url):
    """The page in a chromeless app window, or in the default browser if there is no Chrome/Edge."""
    cmd = window_command(url)
    if cmd:
        subprocess.Popen(cmd)
        return
    print("Chrome/Edge not found; opening the page in your default browser instead.")
    webbrowser.open(url)


def _open_when_ready(url):
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    open_page(url)


def _rip_button_command():
    from ripper_queue.popup import gui_python
    return [gui_python(), str(ROOT / "rip_button_window.py"), "--parent", str(os.getpid()), "--url", APP_URL]


def _spawn_rip_button():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(_rip_button_command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def start_rip_button():
    """The floating RIPPER button over Traktor (its own process; it exits when this app does). A watchdog starts it again if it ever
    dies (at most 5 times a minute). Windows only; RIPPER_RIP_BUTTON=0 turns it off."""
    if sys.platform != "win32" or os.environ.get("RIPPER_RIP_BUTTON") == "0":
        return None
    proc = _spawn_rip_button()

    def watch(current):
        starts = [time.monotonic()]
        while True:
            time.sleep(3)
            if current.poll() is None:
                continue
            now = time.monotonic()
            starts = [t for t in starts if now - t < 60]
            if len(starts) >= 5:   # it keeps dying: stop, rather than loop
                return
            starts.append(now)
            current = _spawn_rip_button()

    threading.Thread(target=watch, args=(proc,), daemon=True).start()
    return proc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", action="store_true", help="auto-reload server and browser (development only)")
    ap.add_argument("--no-window", action="store_true", help="do not open a window, just serve")
    args = ap.parse_args(argv)

    for line in checkout_report():
        print(line)

    if args.dev:
        if importlib.util.find_spec("watchfiles") is None:
            sys.exit("--dev needs watchfiles: pip install -r requirements-dev.txt")
        os.environ["RIPPER_DEV"] = "1"  # inherited by the reloaded server process
        print("Dev mode: server reloads on .py changes; browser reloads on template/static changes.")
        for name, value in dev_defaults().items():
            print(f"Dev mode: {name} = {value} (separate from your real data)")

    if not args.no_window:
        threading.Thread(target=_open_when_ready, args=(APP_URL,), daemon=True).start()
        start_rip_button()
    uvicorn.run("app:app", **uvicorn_kwargs(args.dev))


if __name__ == "__main__":
    main()
