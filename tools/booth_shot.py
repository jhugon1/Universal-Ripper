#!/usr/bin/env python3
"""tools/booth_shot.py - screenshot /booth with headless Chrome/Edge (no extra dependencies).

  python tools/booth_shot.py                      all shots into <tmp>/ripper_booth_shots
  python tools/booth_shot.py --only warn stale    just these
  python tools/booth_shot.py --out some/folder

Starts the app on spare ports with a throwaway database, feeds it events with tools/fake_traktor.py
(timelines are applied instantly, up to a chosen point), and saves one PNG per shot."""
import argparse
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_traktor as ft  # noqa: E402
import run  # noqa: E402

WIDE, PHONE = (1280, 720), (390, 844)


def warn_timeline():
    tl = ft.Timeline()
    ft.startup(tl)
    tl.at(0.5, "/deckLoaded/A", ft.loaded(ft.track("A"), "Almost Over", "Test Artist A", 124.0, 100.0))
    tl.at(0.6, "/updateDeck/A", {"elapsedTime": 82.0, "isPlaying": True})
    tl.at(0.7, "/deckLoaded/B", ft.loaded(ft.track("B"), "Just Started", "Test Artist B", 128.0, 200.0, tempo=1.04))
    tl.at(0.8, "/updateDeck/B", {"elapsedTime": 14.0, "isPlaying": True})
    return tl


# name -> (timeline factory, apply events up to this scenario time, viewport, extra real seconds to wait)
SHOTS = {
    "startup": (ft.SCENARIOS["startup"], 1.0, WIDE, 0),
    "play_a": (ft.SCENARIOS["play_a"], 10.0, WIDE, 0),
    "paused": (ft.SCENARIOS["play_a"], 34.0, WIDE, 0),
    "crossfade": (ft.SCENARIOS["crossfade_a_b"], 20.0, WIDE, 0),
    "all_decks": (ft.SCENARIOS["all_decks"], 10.0, WIDE, 0),
    "warn": (warn_timeline, 1.0, WIDE, 0),
    "long_titles": (ft.SCENARIOS["long_titles"], 6.0, WIDE, 0),
    "long_titles_phone": (ft.SCENARIOS["long_titles"], 6.0, PHONE, 0),
    "all_decks_phone": (ft.SCENARIOS["all_decks"], 10.0, PHONE, 0),
    "stale": (ft.SCENARIOS["disconnect"], 10.0, WIDE, 7),
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def events_until(timeline, until):
    t = 0.0
    for delay, path, body in timeline.events():
        t += delay
        if t > until:
            break
        yield 0.0, path, body


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "ripper_booth_shots"))
    ap.add_argument("--only", nargs="+", choices=sorted(SHOTS), help="shots to take (default: all)")
    args = ap.parse_args()
    browser = run.find_chromium()
    if not browser:
        sys.exit("no Chrome or Edge found")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="booth_shot_"))
    db = work / "shot.db"
    app_port, recv_port = free_port(), free_port()
    env = dict(os.environ, RIPPER_DB=str(db), RIPPER_TRAKTOR_PORT=str(recv_port))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(app_port), "--log-level", "warning"],
                            cwd=REPO, env=env)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{app_port}/api/queue", timeout=1).read()
                break
            except OSError:
                time.sleep(0.2)
        else:
            sys.exit("app did not start")
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO requests (artist, song, status) VALUES ('Test Artist N', 'Next Up Song', 'downloaded')")
        conn.commit()
        conn.close()

        post = ft.make_poster(f"http://127.0.0.1:{recv_port}")
        for name in args.only or list(SHOTS):
            make, until, (w, h), wait = SHOTS[name]
            tl = ft.Timeline()
            ft.startup(tl)  # resets every deck to empty first
            ft.run_events(tl.events(), post, sleep=lambda s: None)
            ft.run_events(events_until(make(), until), post, sleep=lambda s: None)
            time.sleep(wait + 0.6)  # let the app push and persist
            path = out / f"{name}.png"
            url = f"http://127.0.0.1:{app_port}/booth?poll"  # polling: an open SSE stream stalls headless Chrome
            if w < 500:  # headless Chrome will not make a window narrower than ~500 px: use a framed page instead
                frame = work / f"{name}_frame.html"
                frame.write_text(f'<body style="margin:0;background:#000"><iframe src="{url}" '
                                 f'style="width:{w}px;height:{h}px;border:0"></iframe>', encoding="utf-8")
                url, w = frame.as_uri(), 500
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--user-data-dir={work / 'profile'}", f"--window-size={w},{h}",
                            "--virtual-time-budget=3000", f"--screenshot={path}", url],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            print(f"{name:18} {path}" if path.exists() else f"{name:18} FAILED")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
