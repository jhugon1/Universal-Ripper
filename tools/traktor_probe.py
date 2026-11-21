#!/usr/bin/env python3
"""tools/traktor_probe.py - receiver for ErikMinekus's Traktor API-client mod (it POSTs JSON to localhost:8080).
Records everything so we can build + test against REAL Traktor 4 data later.
  python tools/traktor_probe.py            listen on :8080
  python tools/traktor_probe.py --demo     send fake events to a running probe (no Traktor needed)
Output: fixtures/traktor/session_<time>.jsonl (every request) and fixtures/traktor/samples/ (first of each shape)."""
import argparse, json, sys, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "traktor"
decks, chans, clock, seen = {}, {}, {}, set()
LOG = None


def label(d):
    return f"{d.get('artist') or '?'} - {d.get('title') or '?'}"


def handle(path, body):
    parts = path.strip("/").split("/")
    kind, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    if kind in ("deckLoaded", "updateDeck"):
        before = dict(decks.get(arg, {})); decks.setdefault(arg, {}).update(body); d = decks[arg]
        if kind == "deckLoaded":
            return f"DECK {arg} LOADED  {label(d)} | {d.get('bpm')} bpm | {d.get('filePath')}"
        for k in ("isPlaying", "isSynced"):
            if k in body and body[k] != before.get(k):
                return f"DECK {arg} {k}={body[k]}  ({label(d)})"
    elif kind == "updateChannel":
        before = chans.get(arg, {}).get("isOnAir"); chans.setdefault(arg, {}).update(body)
        if "isOnAir" in body and body["isOnAir"] != before:
            live = [f"ch{c}" for c, v in sorted(chans.items()) if v.get("isOnAir")]
            return f"CHANNEL {arg} isOnAir={body['isOnAir']}  -> on air now: {', '.join(live) or 'nothing'}"
    elif kind == "updateMasterClock":
        clock.update(body); return f"MASTER CLOCK deck={body.get('deck')} bpm={body.get('bpm')}"
    return None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):  # open http://localhost:8080 in a browser to see current state
        self._reply(200, {"decks": decks, "channels": chans, "masterClock": clock})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try: body = json.loads(raw or b"{}")
        except Exception: body = {"_raw": raw.decode("utf-8", "replace")}
        rec = {"t": time.time(), "path": self.path, "body": body}
        LOG.write(json.dumps(rec) + "\n"); LOG.flush()
        shape = (self.path.strip("/").split("/")[0], tuple(sorted(body)))
        if shape not in seen:
            seen.add(shape)
            (OUT / "samples").mkdir(parents=True, exist_ok=True)
            (OUT / "samples" / f"{len(seen):02d}_{shape[0]}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        line = handle(self.path, body)
        if line: print(time.strftime("%H:%M:%S"), line, flush=True)
        self._reply(200, {"ok": True})


def demo(port):
    home = Path.home()
    evs = [("deckLoaded/A", {"filePath": str(home / "Music/Traktor/Daft Punk - One More Time.mp3"), "title": "One More Time", "artist": "Daft Punk", "bpm": 122.9, "isPlaying": False}),
           ("updateMasterClock", {"deck": "A", "bpm": 122.9}),
           ("updateChannel/1", {"isOnAir": True}),
           ("updateDeck/A", {"isPlaying": True, "elapsedTime": 0.0}),
           ("updateDeck/A", {"elapsedTime": 1.0}),
           ("updateChannel/1", {"onAirLevel": 0.8})]
    for p, b in evs:
        req = urllib.request.Request(f"http://localhost:{port}/{p}", json.dumps(b).encode(), {"Content-Type": "application/json"})
        print("sent", p, "->", urllib.request.urlopen(req, timeout=5).status)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8080); ap.add_argument("--demo", action="store_true"); a = ap.parse_args()
    if a.demo: demo(a.port); sys.exit()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        sys.exit(f"cannot listen on {a.port}: {e}  (something else is using it)")
    OUT.mkdir(parents=True, exist_ok=True)
    LOG = open(OUT / time.strftime("session_%Y%m%d_%H%M%S.jsonl"), "a", encoding="utf-8")
    print(f"listening on http://localhost:{a.port}  | logging to {LOG.name}\nwaiting for Traktor... (Ctrl+C to stop)", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nstopped.")