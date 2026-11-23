"""Cross-platform launcher: web app on 127.0.0.1:8000 (this machine only), then open the page.

  python run.py             start everything and open the page in an app window (Chrome or Edge)
  python run.py --dev       the same with a separate dev database and folders, and the page reloads
                            when .py, template or static files change
  python run.py --no-window start the server only (open http://127.0.0.1:8000/ yourself)
  python run.py --check     report what is ready and what is missing (Python, packages, ffmpeg, browser, Traktor), then exit
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
import urllib.request
import webbrowser
from pathlib import Path

try:
    import uvicorn
except ImportError:   # not set up yet: `python run.py --check` still works and says what to do
    uvicorn = None

PORT = 8000
APP_URL = f"http://127.0.0.1:{PORT}/"  # 127.0.0.1, not localhost: the Spotify login returns to this exact address
ROOT = Path(__file__).resolve().parent


REQUIRED_PACKAGES = ("fastapi", "uvicorn", "yt_dlp", "ytmusicapi", "jinja2", "multipart", "mutagen", "PIL")


def refresh_windows_path():
    """Windows: add the PATH entries that were created after this program's parent started (for example ffmpeg installed a minute
    ago by Setup), so a double-clicked launcher sees them without signing out."""
    if os.name != "nt":
        return
    try:
        import winreg
        parts = []
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "Path")
                    parts += [os.path.expandvars(x) for x in value.split(os.pathsep) if x]
            except OSError:
                pass
        have = {x.lower() for x in os.environ.get("PATH", "").split(os.pathsep)}
        extra = [x for x in parts if x.lower() not in have]
        if extra:
            os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + os.pathsep.join(extra)
    except Exception:
        pass


def app_is_running(port=PORT) -> bool:
    """True when a Ripped Ripper server already answers on the port (GET /api/popup is ours)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/popup", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def traktor_dirs():
    """Traktor Pro install folders found on this machine (newest first)."""
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)")]
    return [Path(r) / "Native Instruments" / name for r in roots if r for name in ("Traktor Pro 4", "Traktor Pro 3")
            if (Path(r) / "Native Instruments" / name).is_dir()]


def traktor_mod_installed(traktor_dir) -> bool:
    """True when the deck-data mod is in this Traktor's D2 mapping."""
    d2 = Path(traktor_dir) / "Resources64" / "qml" / "CSI" / "D2" / "D2.qml"
    try:
        return "ApiModule" in d2.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def environment_report(port=PORT):
    """[(ok, required, text)] for `--check`: what a working install needs, and what is only nice to have."""
    rows = []
    py_ok = sys.version_info >= (3, 10)
    rows.append((py_ok, True, f"Python {sys.version_info.major}.{sys.version_info.minor}" + ("" if py_ok else " (3.10 or newer is needed)")))
    missing = [m for m in REQUIRED_PACKAGES if importlib.util.find_spec(m) is None]
    rows.append((not missing, True, "packages installed" if not missing else "packages missing: " + ", ".join(missing) + " (run Setup.bat)"))
    rows.append((importlib.util.find_spec("tkinter") is not None, True,
                 "tkinter (the RIPPER button, panel and pop-up)" if importlib.util.find_spec("tkinter") else
                 "tkinter missing: reinstall Python and keep the 'tcl/tk and IDLE' option ticked"))
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        rows.append((bool(found), True, f"{tool} found" if found else f"{tool} not found (Setup.bat installs it with winget, or see the README)"))
    browser = find_chromium()
    rows.append((bool(browser), False, "Chrome/Edge found (the Ripper opens in its own window)" if browser else
                 "no Chrome/Edge: the page will open in your default browser instead"))
    if app_is_running(port):
        rows.append((True, False, f"Ripped Ripper is running right now (port {port})"))
    else:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                rows.append((False, True, f"port {port} is used by another program"))
        except OSError:
            rows.append((True, True, f"port {port} is free"))
    dirs = traktor_dirs()
    if not dirs:
        rows.append((True, False, "Traktor Pro not found in Program Files (the Ripper still works on its own)"))
    else:
        installed = traktor_mod_installed(dirs[0])
        rows.append((installed, False, f"{dirs[0].name}: deck-sync mod " + ("installed" if installed else
                     "not installed (optional: double-click traktor_mod\\Install Traktor Mod.bat)")))
    return rows


def print_environment_report(port=PORT) -> bool:
    """Print the report; True when everything required is fine."""
    all_ok = True
    for ok, required, text in environment_report(port):
        mark = "ok" if ok else ("!!" if required else "--")
        all_ok = all_ok and (ok or not required)
        print(f"  [{mark}] {text}")
    print("Everything needed is in place." if all_ok else "Something needed is missing: see the [!!] lines above.")
    return all_ok


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
    ap.add_argument("--check", action="store_true", help="report what is ready and what is missing, then exit")
    args = ap.parse_args(argv)

    refresh_windows_path()
    if args.check:
        sys.exit(0 if print_environment_report() else 1)
    if uvicorn is None:
        sys.exit("Some packages are missing, so the Ripper cannot start yet. Run Setup.bat (it installs everything), "
                 "then start the Ripper again.")
    if app_is_running():   # a second double-click just brings up the window of the one that is already running
        print("Ripped Ripper is already running; opening its window.")
        if not args.no_window:
            open_page(APP_URL)
        return

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
