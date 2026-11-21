"""Cross-platform launcher: web app on :8000, optional ngrok, open the DJ dashboard."""
import shutil
import socket
import subprocess
import threading
import time
import webbrowser

import uvicorn

PORT = 8000
DJ_URL = f"http://localhost:{PORT}/dj"


def _open_when_ready():
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    webbrowser.open(DJ_URL)


def main():
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
        uvicorn.run("app:app", host="0.0.0.0", port=PORT)
    finally:
        if ngrok:
            ngrok.terminate()


if __name__ == "__main__":
    main()
