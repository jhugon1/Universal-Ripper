#!/usr/bin/env python3
"""tools/traktor_check.py - guided, self-grading test of the Traktor integration (tests A + B).
Automates everything except clicks inside Traktor: preflight checks, receiver on :8080, launching Traktor,
live detection of each step, PASS/FAIL grading, deck->channel mapping, scrubbed fixtures, printed summary.
  python tools/traktor_check.py               run the guided test (Windows)
  python tools/traktor_check.py --no-launch   don't start Traktor for you
  python tools/traktor_check.py --selftest    simulated Traktor (no Traktor needed; checks the harness itself)
During a step: Enter = skip it, q = quit."""
import argparse, json, os, plistlib, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
PF = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Native Instruments" / "Traktor Pro 4"
D2 = PF / "Resources64" / "qml" / "CSI" / "D2"
XML = HOME / "Music" / "iTunes" / "iTunes Music Library.xml"
OUT = REPO / "fixtures" / "traktor"
PORT = 8080
# keys ApiDeck.qml sends on deckLoaded (mod commit 1caf11f). JSON.stringify drops undefined values, so a key that
# is MISSING here means that Traktor property path no longer exists in this Traktor version.
LOADED_KEYS = ["filePath", "title", "artist", "album", "genre", "comment", "comment2", "label", "mix", "remixer", "key",
               "keyText", "gridOffset", "trackLength", "elapsedTime", "nextCuePos", "nextCueName", "nextCueType",
               "prevCuePos", "prevCueName", "prevCueType", "bpm", "tempo", "resultingKey", "isPlaying", "isSynced", "isKeyLockOn"]

EVENTS, LOCK = [], threading.Lock()
RESULTS, LINES = [], []


def say(s=""):
    print(s, flush=True); LINES.append(s)


def result(name, status, detail=""):
    RESULTS.append((name, status, detail)); say(f"  -> {status}: {name}" + (f"  ({detail})" if detail else ""))


# ---------------- receiver ----------------
class H(BaseHTTPRequestHandler):
    log = None

    def log_message(self, *a): pass

    def do_GET(self):
        with LOCK: data = json.dumps({"events": len(EVENTS)}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try: body = json.loads(raw or b"{}")
        except Exception: body = {"_raw": raw.decode("utf-8", "replace")}
        parts = self.path.strip("/").split("/")
        ev = {"t": time.time(), "path": self.path, "kind": parts[0], "arg": parts[1] if len(parts) > 1 else "", "body": body}
        with LOCK: EVENTS.append(ev)
        if H.log: H.log.write(json.dumps(ev) + "\n"); H.log.flush()
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")


def events_since(t0, kind=None):
    with LOCK: return [e for e in EVENTS if e["t"] >= t0 and (kind is None or e["kind"] == kind)]


# ---------------- waiting with skip/quit ----------------
def key_pressed():
    if os.name != "nt": return None
    import msvcrt
    if msvcrt.kbhit():
        c = msvcrt.getwch()
        return "q" if c.lower() == "q" else "skip" if c in "\r\n" else None
    return None


def wait_for(pred, timeout, label):
    t_end, last = time.time() + timeout, -1
    while time.time() < t_end:
        hit = pred()
        if hit: return hit
        k = key_pressed()
        if k == "q": raise KeyboardInterrupt
        if k == "skip": return "skip"
        left = int(t_end - time.time())
        if left % 15 == 0 and left != last:
            last = left; print(f"     ...waiting for {label} ({left}s left, Enter=skip, q=quit)", flush=True)
        time.sleep(0.2)
    return None


# ---------------- preflight ----------------
def preflight(selftest):
    say("== PREFLIGHT ==")
    if not selftest:
        ok = (D2 / "D2.qml").exists() and "ApiModule" in (D2 / "D2.qml").read_text(errors="ignore") and (D2 / "Api" / "ApiClient.js").exists()
        result("mod installed in Traktor Pro 4 D2 mapping", "PASS" if ok else "FAIL", str(D2) if not ok else "")
        if ok:
            m = re.search(r'API_BASE_URL\s*=\s*"([^"]+)"', (D2 / "Api" / "ApiClient.js").read_text(errors="ignore"))
            result("mod posts to localhost:8080", "PASS" if m and m.group(1).rstrip("/").endswith(":8080") else "FAIL", m.group(1) if m else "no URL")
    paths = []
    if XML.exists():
        try:
            lib = plistlib.loads(XML.read_bytes())
            tracks = lib["Tracks"]
            pl = next(p for p in lib["Playlists"] if p["Name"] == "Ripper Queue")
            paths = [Path(urllib.parse.unquote(urllib.parse.urlparse(tracks[str(i["Track ID"])]["Location"]).path).lstrip("/")
                          if os.name == "nt" else urllib.parse.unquote(urllib.parse.urlparse(tracks[str(i["Track ID"])]["Location"]).path))
                     for i in pl["Playlist Items"]]
            missing = [p for p in paths if not p.exists()]
            result("queue XML parses, playlist 'Ripper Queue' present", "PASS", f"{len(paths)} tracks")
            result("queue XML tracks exist on disk", "PASS" if not missing else "FAIL", ", ".join(map(str, missing)))
        except Exception as e:
            result("queue XML parses", "FAIL", f"{type(e).__name__}: {e}")
    else:
        result("queue XML at Traktor's iTunes/Music path", "FAIL", f"missing {XML}")
    if not selftest:
        tsi = next(iter(sorted((HOME / "Documents" / "Native Instruments").glob("Traktor 4*/Traktor Settings.tsi"))), None)
        if tsi:
            import base64
            txt = tsi.read_text(errors="ignore"); blob = txt
            for b in re.findall(r'"([A-Za-z0-9+/=]{200,})"', txt):
                try: blob += base64.b64decode(b).decode("latin-1")
                except Exception: pass
            hit = "D2" in blob.replace("\x00", "")
            result("virtual Kontrol D2 device present (heuristic, settings saved on Traktor exit)", "LIKELY" if hit else "NOT SEEN")
    return paths


def traktor_running():
    if os.name != "nt": return False
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout.lower()
    return "traktor" in out


# ---------------- the guided steps ----------------
def run_steps(queue_paths):
    say("\n== LIVE TEST (do each action in Traktor; detection is automatic) ==")
    norm = lambda p: str(p).replace("/", "\\").lower() if os.name == "nt" else str(p)
    qset = {norm(p) for p in queue_paths}

    # 1. mod alive
    say("\nSTEP 1  Mod alive. If you have not yet: Preferences > Controller Manager > Add... > Traktor > Kontrol D2.\n"
        "        (Any deck/mixer activity also counts. Nudge a channel fader if nothing arrives.)")
    t0 = time.time()
    hit = wait_for(lambda: events_since(0), 180, "any POST from the mod")
    if hit in (None, "skip"):
        result("mod is sending data", "FAIL" if hit is None else "SKIP", "no POST received")
        log_hints(); return
    result("mod is sending data", "PASS", f"first: /{hit[0]['kind']}/{hit[0]['arg']}".rstrip("/"))

    # 2. load from Ripper Queue into deck A
    say("\nSTEP 2  Browser: right-click the iTunes node > Refresh Playlists > open 'Ripper Queue' > load any track into DECK A.")
    t0 = time.time()
    hit = wait_for(lambda: [e for e in events_since(t0, "deckLoaded") if e["arg"] == "A" and (e["body"].get("filePath") or e["body"].get("title"))], 240, "deckLoaded/A with a track")
    if hit in (None, "skip"):
        result("track loaded into deck A", "FAIL" if hit is None else "SKIP")
    else:
        b = hit[-1]["body"]; fp = b.get("filePath", "")
        result("track loaded into deck A", "PASS", f"{b.get('artist') or '?'} - {b.get('title') or '?'}")
        result("filePath is a usable local path that exists", "PASS" if fp and Path(fp).exists() else "FAIL", fp or "empty")
        result("loaded track is from the Ripper Queue XML (test A)", "PASS" if norm(fp) in qset else "FAIL",
               "path matches a queue entry" if norm(fp) in qset else "path not in the queue XML (loaded from elsewhere?)")
        missing = [k for k in LOADED_KEYS if k not in b]; nulls = [k for k in LOADED_KEYS if k in b and b[k] is None]
        result("deckLoaded has every field the mod sends", "PASS" if not missing else "WARN",
               ("missing (property path gone in Traktor 4?): " + ", ".join(missing)) if missing else f"{len(b)} fields")
        if nulls: say(f"     null fields (normal for cue fields / untagged files): {', '.join(nulls)}")

    # 3. play deck A
    say("\nSTEP 3  Press PLAY on deck A and let it run ~3 seconds.")
    t0 = time.time()
    hit = wait_for(lambda: [e for e in events_since(t0, "updateDeck") if e["arg"] == "A" and e["body"].get("isPlaying") is True], 120, "deck A isPlaying=true")
    if hit in (None, "skip"):
        result("play state reported", "FAIL" if hit is None else "SKIP")
    else:
        result("play state reported", "PASS")
        time.sleep(3.2)
        ticks = [e["body"]["elapsedTime"] for e in events_since(t0, "updateDeck") if e["arg"] == "A" and "elapsedTime" in e["body"]]
        result("elapsedTime ticks while playing", "PASS" if len(ticks) >= 2 and ticks[-1] > ticks[0] else "FAIL", f"{len(ticks)} updates")

    # 4. fader -> channel mapping
    say("\nSTEP 4  Pull deck A's CHANNEL FADER fully down, then back up.")
    t0 = time.time()
    hit = wait_for(lambda: [e for e in events_since(t0, "updateChannel") if e["body"].get("isOnAir") is False], 120, "a channel going off air")
    if hit in (None, "skip"):
        result("on-air state reported per channel", "FAIL" if hit is None else "SKIP")
    else:
        ch = hit[0]["arg"]
        result("on-air state reported per channel", "PASS", f"deck A fader -> mixer channel {ch}")
        result("deck A maps to channel 1 (assumed mapping)", "PASS" if ch == "1" else "WARN", f"observed channel {ch}")
        back = wait_for(lambda: [e for e in events_since(hit[0]["t"], "updateChannel") if e["arg"] == ch and e["body"].get("isOnAir") is True], 60, "fader back up")
        result("channel back on air after fader up", "PASS" if back not in (None, "skip") else "FAIL" if back is None else "SKIP")

    # 5. optional deck B
    say("\nSTEP 5  (optional, Enter to skip) Load any track into DECK B and pull ITS channel fader down.")
    t0 = time.time()
    hit = wait_for(lambda: [e for e in events_since(t0, "updateChannel") if e["body"].get("isOnAir") is False], 120, "deck B fader")
    if hit not in (None, "skip"):
        result("deck B channel mapping", "PASS" if hit[0]["arg"] == "2" else "WARN", f"deck B fader -> mixer channel {hit[0]['arg']}")
    else:
        result("deck B channel mapping", "SKIP")
    clock = events_since(0, "updateMasterClock")
    result("master clock reported", "PASS" if clock else "WARN", f"deck={clock[-1]['body'].get('deck')} bpm={clock[-1]['body'].get('bpm')}" if clock else "none seen")


def log_hints():
    logs = sorted((HOME / "Documents" / "Native Instruments").glob("Traktor 4*/Logs/*"), key=lambda p: p.stat().st_mtime)
    if not logs: return
    say(f"     newest Traktor log: {logs[-1].name}; lines mentioning qml/D2/Api/error:")
    for ln in logs[-1].read_text(errors="ignore").splitlines()[-400:]:
        if re.search(r"qml|\bD2\b|Api|error", ln, re.I): say("       " + ln[:200])


# ---------------- fixtures ----------------
def scrub(o):
    if isinstance(o, dict): return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, list): return [scrub(v) for v in o]
    if isinstance(o, str):
        for a, b in ((str(HOME), "%USERPROFILE%"), (str(HOME).replace("\\", "/"), "%USERPROFILE%"), (HOME.name, "USER")):
            o = o.replace(a, b)
    return o


def write_samples():
    seen, n = set(), 0
    (OUT / "samples").mkdir(parents=True, exist_ok=True)
    with LOCK: evs = list(EVENTS)
    for e in evs:
        shape = (e["kind"], tuple(sorted(e["body"])))
        if shape in seen: continue
        seen.add(shape); n += 1
        (OUT / "samples" / f"t4_{n:02d}_{e['kind']}.json").write_text(json.dumps(scrub({"path": e["path"], "body": e["body"]}), indent=2), encoding="utf-8")
    return n, len(evs)


# ---------------- selftest: fake Traktor ----------------
def fake_traktor(paths):
    def post(p, b):
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{PORT}/{p}", json.dumps(b).encode(), {"Content-Type": "application/json"}), timeout=5)
    time.sleep(0.5); post("updateMasterClock", {"deck": None, "bpm": 120.0})
    time.sleep(0.5)
    body = {k: None for k in LOADED_KEYS}; body.update(filePath=str(paths[0]) if paths else "", title="One More", artist="", bpm=123.0, isPlaying=False)
    post("deckLoaded/A", body)
    time.sleep(0.5); post("updateDeck/A", {"isPlaying": True, "elapsedTime": 0.0})
    for s in (1.0, 2.0, 3.0): time.sleep(1.0); post("updateDeck/A", {"elapsedTime": s})
    time.sleep(0.5); post("updateChannel/1", {"isOnAir": False}); time.sleep(0.3); post("updateChannel/1", {"isOnAir": True})
    time.sleep(0.5); post("updateChannel/2", {"isOnAir": False})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-launch", action="store_true"); ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    say(f"traktor_check  {time.strftime('%Y-%m-%d %H:%M:%S')}  {'(SELFTEST: simulated Traktor)' if a.selftest else ''}")
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError as e:
        sys.exit(f"port {PORT} busy ({e}). Stop traktor_probe.py or whatever else is on it, then rerun.")
    OUT.mkdir(parents=True, exist_ok=True)
    H.log = open(OUT / time.strftime("session_check_%Y%m%d_%H%M%S.jsonl"), "a", encoding="utf-8")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    paths = preflight(a.selftest)
    say(f"\nreceiver listening on http://localhost:{PORT}")
    if a.selftest:
        threading.Thread(target=fake_traktor, args=(paths,), daemon=True).start()
    elif traktor_running():
        say("Traktor is already running. (If you installed the mod while it was open, restart Traktor.)")
    elif not a.no_launch and (PF / "Traktor Pro 4.exe").exists():
        say("starting Traktor..."); subprocess.Popen([str(PF / "Traktor Pro 4.exe")], cwd=str(PF))
    else:
        say("start Traktor now.")
    try:
        run_steps(paths)
    except KeyboardInterrupt:
        say("\n(quit early)")
    n, total = write_samples()
    srv.shutdown()
    say("\n== SUMMARY ==")
    for name, st, d in RESULTS: say(f"  {st:8} {name}" + (f"  [{d}]" if d else ""))
    say(f"\n{total} events captured; {n} scrubbed samples in fixtures/traktor/samples/t4_*.json; raw log {Path(H.log.name).name} (gitignored)")


if __name__ == "__main__":
    main()
