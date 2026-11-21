"""Cross-platform launcher: web app on :8000, optional ngrok, open the DJ dashboard.

  python run.py          normal start
  python run.py --dev    reload the server when .py files change and reload open browser tabs
                         when the server restarts or a template/static file changes
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
DJ_URL = f"http://localhost:{PORT}/dj"
ROOT = Path(__file__).resolve().parent


def uvicorn_kwargs(dev: bool) -> dict:
    kwargs = {"host": "0.0.0.0", "port": PORT}
    if dev:
        # watchfiles (requirements-dev.txt) skips .venv; uvicorn's fallback reloader would scan it.
        kwargs.update(
            reload=True,
            reload_dirs=[str(ROOT)],
            reload_includes=["*.py"],
            reload_excludes=["tests/*", "tools/*", "fixtures/*"],
        )
    return kwargs


def _open_when_ready():
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(DJ_URL)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", action="store_true", help="auto-reload server and browser (development only)")
    args = ap.parse_args(argv)

    if args.dev:
        if importlib.util.find_spec("watchfiles") is None:
            sys.exit("--dev needs watchfiles: pip install -r requirements-dev.txt")
        os.environ["RIPPER_DEV"] = "1"  # inherited by the reloaded server process
        print("Dev mode: server reloads on .py changes; browser reloads on template/static changes.")

    ngrok = None
    if shutil.which("ngrok"):
        print("Starting ngrok tunnel...")
        ngrok = subprocess.Popen(
            ["ngrok", "http", str(PORT), "--log=stdout"],
            stdout=subprocess.DEVNULL,
        )
        print("ngrok URL: see http://127.0.0.1:4040")
    else:
        print("ngrok not found on PATH; running locally only.")

    threading.Thread(target=_open_when_ready, daemon=True).start()
    try:
        uvicorn.run("app:app", **uvicorn_kwargs(args.dev))
    finally:
        if ngrok:
            ngrok.terminate()


if __name__ == "__main__":
    main()
