#!/usr/bin/env python3
"""tools/fake_traktor.py - post mod-format events to the Traktor receiver, no Traktor needed.

  python tools/fake_traktor.py --list
  python tools/fake_traktor.py --scenario crossfade_a_b --loop
  python tools/fake_traktor.py replay fixtures/traktor/replays/capture_startup_load_play.jsonl --speed 4
  python tools/fake_traktor.py replay fixtures/traktor/samples          (a folder of samples, fixed gaps)

The app listens on 127.0.0.1:8080 by default (RIPPER_TRAKTOR_PORT). Stop tools/traktor_probe.py and the
real mod first if they hold that port. --speed N runs the timeline N times faster (elapsedTime then
advances N times faster than real time too). Standard library only."""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8080"  # 127.0.0.1, not localhost: Windows tries ::1 first (about 2 s)
TRACK_DIR = "C:\\Music\\Traktor\\Ripper Queue\\"
LOADED_KEYS = ["filePath", "title", "artist", "album", "genre", "comment", "comment2", "label", "mix", "remixer",
               "key", "keyText", "gridOffset", "trackLength", "elapsedTime", "nextCuePos", "nextCueName",
               "nextCueType", "prevCuePos", "prevCueName", "prevCueType", "bpm", "tempo", "resultingKey",
               "isPlaying", "isSynced", "isKeyLockOn"]


# ---------------- timelines ----------------
class Timeline:
    """Collects (time, path, body) in any order; `events()` yields (delay_since_previous, path, body)."""

    def __init__(self):
        self._items = []

    def at(self, t, path, body):
        self._items.append((t, len(self._items), path, body))
        return self

    def ticks(self, deck, start, seconds, elapsed_from=0.0, tempo=1.0):
        """The mod's once-a-second elapsedTime updates while a deck plays."""
        for i in range(1, int(seconds) + 1):
            self.at(start + i, f"/updateDeck/{deck}", {"elapsedTime": elapsed_from + i * tempo})
        return self

    def events(self):
        prev = 0.0
        for t, _, path, body in sorted(self._items):
            yield max(0.0, t - prev), path, body
            prev = t


def startup(tl, t=0.0):
    """Within the first second after the mapping loads: every deck empty, every channel 'on air'."""
    for i, d in enumerate("ABCD"):
        tl.at(t + 0.02 * i, f"/deckLoaded/{d}", loaded("", "", "", 120.0, 0.0))
    for c in "1234":
        tl.at(t + 0.2, f"/updateChannel/{c}", {"isOnAir": True})
        tl.at(t + 0.2, f"/updateChannel/{c}", {"onAirLevel": 1})
    tl.at(t + 0.3, "/updateMasterClock", {"deck": None, "bpm": 120})


def loaded(path, title, artist, bpm, length, key="8m", playing=False, tempo=1.0):
    body = {k: None for k in LOADED_KEYS}
    body.update(filePath=path, title=title, artist=artist, bpm=bpm, key=key, resultingKey=key, trackLength=length,
                elapsedTime=0, tempo=tempo, isPlaying=playing, isSynced=False, isKeyLockOn=False, album="",
                genre="", comment="", comment2="", label="", mix="", remixer="", keyText="", gridOffset=0)
    return body


def track(n, name=None):
    return f"{TRACK_DIR}Test Artist {n} - {name or 'Test Track ' + n}.mp3"


def fade(tl, start, seconds, ch_out, ch_in, steps=16):
    """Crossfade: ch_out 1 -> 0 and ch_in 0 -> 1 in even steps."""
    tl.at(start, f"/updateChannel/{ch_in}", {"isOnAir": True})
    for i in range(1, steps + 1):
        t, x = start + seconds * i / steps, i / steps
        tl.at(t, f"/updateChannel/{ch_out}", {"onAirLevel": round(1 - x, 3)})
        tl.at(t, f"/updateChannel/{ch_in}", {"onAirLevel": round(x, 3)})
    tl.at(start + seconds, f"/updateChannel/{ch_out}", {"isOnAir": False})


# ---------------- scenarios ----------------
def scn_startup():
    """Traktor starts: empty loads for all decks, all channels on air. Nothing should show as playing."""
    tl = Timeline()
    startup(tl)
    return tl


def scn_play_a():
    """Load a track on deck A, play it for 30 s, then pause."""
    tl = Timeline()
    startup(tl)
    tl.at(1.5, "/deckLoaded/A", loaded(track("A"), "Test Track A", "Test Artist A", 124.0, 90.0, "8m"))
    tl.at(3.0, "/updateDeck/A", {"isPlaying": True})
    tl.at(3.0, "/updateMasterClock", {"deck": "A", "bpm": 124.0})
    tl.ticks("A", 3.0, 30)
    tl.at(33.5, "/updateDeck/A", {"isPlaying": False})
    tl.at(33.5, "/updateMasterClock", {"deck": None, "bpm": 124.0})
    return tl


def scn_crossfade_a_b():
    """A plays, B starts and is faded in over 8 s, A is faded out and stops, B plays on."""
    tl = Timeline()
    startup(tl)
    tl.at(1.0, "/deckLoaded/A", loaded(track("A"), "Test Track A", "Test Artist A", 124.0, 60.0, "8m"))
    tl.at(1.2, "/deckLoaded/B", loaded(track("B"), "Test Track B", "Test Artist B", 128.0, 75.0, "9m"))
    tl.at(1.5, "/updateChannel/2", {"onAirLevel": 0})
    tl.at(1.5, "/updateChannel/2", {"isOnAir": False})
    tl.at(2.0, "/updateDeck/A", {"isPlaying": True})
    tl.ticks("A", 2.0, 26)
    tl.at(12.0, "/updateDeck/B", {"isPlaying": True})  # cued, fader still down
    tl.ticks("B", 12.0, 24)
    fade(tl, 16.0, 8.0, "1", "2")
    tl.at(24.5, "/updateDeck/A", {"isPlaying": False})
    return tl


def scn_all_decks():
    """All four decks loaded; A and B playing at different levels, C and D waiting; B takes over at 10 s."""
    tl = Timeline()
    startup(tl)
    for i, (d, bpm, key, length) in enumerate((("A", 122.0, "8m", 200.0), ("B", 126.0, "9m", 180.0),
                                                ("C", 130.0, "10m", 240.0), ("D", 98.5, "3d", 150.0))):
        tl.at(1.0 + 0.1 * i, f"/deckLoaded/{d}", loaded(track(d), f"Test Track {d}", f"Test Artist {d}", bpm, length, key))
    tl.at(2.0, "/updateChannel/2", {"onAirLevel": 0.6})
    for d in "AB":
        tl.at(3.0, f"/updateDeck/{d}", {"isPlaying": True})
        tl.ticks(d, 3.0, 20)
    tl.at(13.0, "/updateChannel/2", {"onAirLevel": 1})
    tl.at(13.0, "/updateChannel/1", {"onAirLevel": 0.4})
    return tl


def scn_long_titles():
    """A very long title, unicode, and a track with no title or artist at all."""
    tl = Timeline()
    startup(tl)
    long_title = "An Extremely Long Track Title That Keeps Going " * 6 + "(Extended Club Mix)"
    tl.at(1.0, "/deckLoaded/A", loaded(track("A"), long_title, "The Artist With A Very Long Name Featuring Many Guests " * 3, 125.0, 300.0))
    tl.at(1.1, "/deckLoaded/B", loaded(track("B", "Unlabeled Track"), "", "", 0.0, 0.0, key=""))
    tl.at(1.5, "/updateChannel/2", {"onAirLevel": 0})
    tl.at(2.0, "/updateDeck/A", {"isPlaying": True})
    tl.ticks("A", 2.0, 14)
    tl.at(8.0, "/updateDeck/B", {"isPlaying": True})
    tl.ticks("B", 8.0, 8)
    fade(tl, 9.0, 3.0, "1", "2")
    tl.at(13.0, "/deckLoaded/A", loaded(track("A2"), "日本語のタイトル ♪ Ünïcödé 🎧", "Åsa Ñandú", 140.0, 200.0))
    return tl


def scn_disconnect():
    """A plays for 8 s, then Traktor goes silent without pausing (crash or closed). Look for the stale state."""
    tl = Timeline()
    startup(tl)
    tl.at(1.0, "/deckLoaded/A", loaded(track("A"), "Test Track A", "Test Artist A", 124.0, 90.0))
    tl.at(2.0, "/updateDeck/A", {"isPlaying": True})
    tl.ticks("A", 2.0, 8)
    return tl


SCENARIOS = {f[4:]: f for f in globals() if f.startswith("scn_")}
SCENARIOS = {name: globals()[fn] for name, fn in SCENARIOS.items()}


# ---------------- replay ----------------
def load_replay(source, gap=0.3):
    """Events from a .jsonl capture (real timing from 't') or a folder of *.json samples (fixed gaps)."""
    source = Path(source)
    if source.is_dir():
        files = sorted(source.glob("*.json"))
        if not files:
            raise SystemExit(f"no *.json samples in {source}")
        for i, f in enumerate(files):
            e = json.loads(f.read_text(encoding="utf-8"))
            yield (0.0 if i == 0 else gap), e["path"], e["body"]
        return
    prev = None
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        path = e.get("path") or "/" + "/".join(x for x in (e.get("kind"), e.get("arg")) if x)
        t = e.get("t")
        delay = 0.0 if t is None or prev is None else max(0.0, t - prev)
        prev = t if t is not None else prev
        yield delay, path, e.get("body", {})


# ---------------- sending ----------------
def make_poster(base_url, timeout=2.0):
    warned = [0.0]

    def post(path, body):
        req = urllib.request.Request(base_url.rstrip("/") + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Fake-Traktor": "1"})
        try:
            urllib.request.urlopen(req, timeout=timeout).read()
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 403:  # the app is on the real database and says no: stop, do not keep trying
                sys.exit("STOPPED: the app refuses fake events because it is using the REAL database. "
                         "Start it with `python run.py --dev` to test with fake data.")
            return False
        except (urllib.error.URLError, OSError) as exc:
            if time.time() - warned[0] > 5:
                warned[0] = time.time()
                print(f"cannot reach {base_url} ({getattr(exc, 'reason', exc)}); is the app running?", file=sys.stderr)
            return False

    return post


def check_target(base_url, allow_real_db=False, timeout=2.5):
    """Ask the receiver which database its app uses. Refuses (exits) if it is the real one."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
        db = info.get("database") or {}
    except (OSError, ValueError, AttributeError):
        print(f"note: could not check which database {base_url} uses (not running yet, or an older server)", file=sys.stderr)
        return
    if not db:
        print(f"note: {base_url} did not say which database it uses; make sure it is a dev instance", file=sys.stderr)
        return
    if db.get("real"):
        banner = "!" * 72
        if not allow_real_db:
            sys.exit(f"{banner}\nREFUSING to send fake events: {base_url} is using the REAL database ({db.get('name')}).\n"
                     "Fake decks would be saved into it and come back after every restart.\n"
                     "Start the app with `python run.py --dev` (it uses a separate dev database),\n"
                     f"or pass --allow-real-db if you really mean it.\n{banner}")
        print(f"{banner}\nWARNING: sending fake events to the REAL database ({db.get('name')}). Use 'Reset decks' afterwards.\n{banner}",
              file=sys.stderr)


def run_events(events, post, speed=1.0, sleep=time.sleep, verbose=False):
    """Send events with their delays divided by speed. Returns the number sent."""
    sent = 0
    for delay, path, body in events:
        if delay > 0:
            sleep(delay / speed)
        ok = post(path, body)
        sent += 1
        if verbose:
            print(("" if ok else "FAILED ") + path, json.dumps(body)[:110], flush=True)
    return sent


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS, help=f"receiver base URL (default {DEFAULT_URL})")
    common.add_argument("--speed", type=float, default=argparse.SUPPRESS, help="timeline speed factor (default 1)")
    common.add_argument("--loop", action="store_true", default=argparse.SUPPRESS, help="repeat until Ctrl-C")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="print every event")
    common.add_argument("--allow-real-db", action="store_true", default=argparse.SUPPRESS,
                        help="send even if the app uses the real database (not recommended)")
    ap = argparse.ArgumentParser(description=__doc__, parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), help="named scenario to play")
    ap.add_argument("--list", action="store_true", help="list scenarios")
    ap.set_defaults(url=DEFAULT_URL, speed=1.0, loop=False, verbose=False, allow_real_db=False)  # SUPPRESS above keeps flags given before "replay"
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay", parents=[common], help="replay a .jsonl capture or a folder of samples")
    rp.add_argument("source")
    rp.add_argument("--gap", type=float, default=0.3, help="seconds between samples in a folder (default 0.3)")
    args = ap.parse_args(argv)

    if args.list:
        for name, fn in sorted(SCENARIOS.items()):
            print(f"  {name:14} {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    if args.speed <= 0:
        ap.error("--speed must be positive")
    if args.cmd == "replay":
        make = lambda: load_replay(args.source, args.gap)
        label = f"replay {args.source}"
    elif args.scenario:
        make = lambda: SCENARIOS[args.scenario]().events()
        label = f"scenario {args.scenario}"
    else:
        ap.error("pass --scenario NAME, replay SOURCE, or --list")

    check_target(args.url, args.allow_real_db)
    post = make_poster(args.url)
    print(f"{label} -> {args.url} (speed x{args.speed:g}{', looping' if args.loop else ''}); Ctrl-C to stop", flush=True)
    try:
        while True:
            n = run_events(make(), post, args.speed, verbose=args.verbose)
            print(f"sent {n} events", flush=True)
            if not args.loop:
                break
            time.sleep(2.0 / args.speed)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
