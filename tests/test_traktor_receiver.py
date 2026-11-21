import asyncio
import json
import socket
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from traktor.receiver import DEFAULT_PORT, configured_port, create_receiver
from traktor.state import TraktorState

ROOT = Path(__file__).resolve().parent.parent
HTTP = httpx.Client(trust_env=False, timeout=2)  # one kept-alive connection: new httpx connections are slow on Windows
TRACK = r"C:\Music\Traktor\Ripper Queue\Artist - Song.mp3"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mod_events(base):
    """What the mod sends for: startup burst, load deck A, play, fader up."""
    ev = [("deckLoaded/" + d, {"filePath": "", "title": ""}) for d in "ABCD"]
    ev += [(f"updateChannel/{c}", {"isOnAir": True}) for c in "1234"]
    ev += [(f"updateChannel/{c}", {"onAirLevel": 1}) for c in "1234"]
    ev += [("deckLoaded/A", {"filePath": TRACK, "title": "Song", "artist": "Artist", "bpm": 120.0,
                             "tempo": 1, "trackLength": 200.0, "elapsedTime": 0, "isPlaying": False}),
           ("updateDeck/A", {"isPlaying": True})]
    return [(f"{base}/{p}", b) for p, b in ev]


# ---- the receiver app on its own -------------------------------------------------------------
def test_receiver_applies_events_and_answers_empty_json():
    state, seen = TraktorState(), []
    c = TestClient(create_receiver(state, on_change=seen.append))
    r = c.post("/deckLoaded/A", json={"filePath": TRACK, "title": "Song", "trackLength": 200})
    assert r.status_code == 200 and r.json() == {}
    assert state.decks["A"]["title"] == "Song"
    assert seen and any(ch["field"] == "title" for ch in seen[0])
    assert c.post("/updateMasterClock", json={"deck": "A", "bpm": 120}).json() == {}
    assert state.master["deck"] == "A"


def test_receiver_survives_bad_input():
    state = TraktorState()
    c = TestClient(create_receiver(state))
    for body in (b"not json", b"", b"[1,2]", b'{"a":', b"\xff\xfe"):
        r = c.post("/updateDeck/A", content=body, headers={"content-type": "application/json"})
        assert r.status_code == 200 and r.json() == {}
    assert c.post("/mystery/Z", json={"x": 1}).status_code == 200
    assert state.decks["A"]["title"] is None


def test_receiver_survives_a_failing_state(monkeypatch):
    state = TraktorState()
    monkeypatch.setattr(state, "apply", lambda *a, **k: 1 / 0)
    r = TestClient(create_receiver(state)).post("/updateDeck/A", json={"isPlaying": True})
    assert r.status_code == 200 and r.json() == {}


def test_receiver_only_takes_posts():
    c = TestClient(create_receiver(TraktorState()))
    assert c.get("/deckLoaded/A").status_code == 405
    assert c.post("/").status_code in (404, 405)


def test_configured_port():
    assert configured_port({}) == DEFAULT_PORT == 8080
    assert configured_port({"RIPPER_TRAKTOR_PORT": "0"}) == 0
    assert configured_port({"RIPPER_TRAKTOR_PORT": "9090"}) == 9090
    assert configured_port({"RIPPER_TRAKTOR_PORT": "banana"}) == DEFAULT_PORT
    assert configured_port({"RIPPER_TRAKTOR_PORT": "70000"}) == DEFAULT_PORT


# ---- wired into the app ----------------------------------------------------------------------
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(app_module, "traktor_state", TraktorState())
    return app_module


def test_main_app_does_not_serve_receiver_routes(app_env):
    with TestClient(app_env.app) as c:
        for path in ("/deckLoaded/A", "/updateDeck/A", "/updateChannel/1", "/updateMasterClock"):
            assert c.post(path, json={"isPlaying": True}).status_code in (404, 405)
        assert app_env.traktor_state.event_count == 0


def test_listener_to_state_to_sse(app_env, monkeypatch):
    port = free_port()
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", str(port))
    q = asyncio.Queue()
    with TestClient(app_env.app) as c:
        app_env.subscribers.add(q)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(50):  # the listener starts in the background
                try:
                    HTTP.post(f"{base}/updateMasterClock", json={}, timeout=1)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            for url, body in mod_events(base):
                assert HTTP.post(url, json=body, timeout=2).json() == {}

            snap = None
            deadline = time.time() + 5
            while time.time() < deadline and not (snap and snap["now_playing"] == "A"):
                try:
                    msg = json.loads(q.get_nowait().removeprefix("data: "))
                    if msg["event"] == "traktor_state":
                        snap = msg["state"]
                except asyncio.QueueEmpty:
                    time.sleep(0.05)
            assert snap and snap["now_playing"] == "A", "no traktor_state event with deck A playing"
            assert snap["decks"]["A"]["title"] == "Song"
            assert "file_path" not in snap["decks"]["A"] and "C:" not in json.dumps(snap)

            api = c.get("/api/traktor/state").json()
            assert api["now_playing"] == "A" and api["decks"]["A"]["file_name"] == "Artist - Song.mp3"
            assert "server_time" in api
        finally:
            app_env.subscribers.discard(q)


def test_sse_events_are_throttled(app_env, monkeypatch):
    port = free_port()
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", str(port))
    q = asyncio.Queue()
    with TestClient(app_env.app) as c:
        app_env.subscribers.add(q)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    HTTP.post(f"{base}/updateMasterClock", json={}, timeout=1)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            time.sleep(0.4)
            while not q.empty():
                q.get_nowait()
            start = time.time()
            for i in range(40):
                HTTP.post(f"{base}/updateDeck/A", json={"elapsedTime": float(i + 1)}, timeout=2)
            sent_for = time.time() - start
            time.sleep(0.6)
            count = 0
            last = None
            while not q.empty():
                last = json.loads(q.get_nowait().removeprefix("data: "))
                count += 1
            assert 1 <= count <= int(sent_for / 0.25) + 2, (count, sent_for)
            assert last["state"]["decks"]["A"]["elapsed"] == 40.0  # the final state always gets through
        finally:
            app_env.subscribers.discard(q)


def test_busy_port_does_not_break_the_app(app_env, monkeypatch):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        monkeypatch.setenv("RIPPER_TRAKTOR_PORT", str(blocker.getsockname()[1]))
        start = time.time()
        with TestClient(app_env.app) as c:
            assert c.get("/api/queue").status_code == 200
            assert c.get("/api/traktor/state").json()["now_playing"] is None
        assert time.time() - start < 3, "shutdown should not wait for the bind retry"


def test_new_sse_client_gets_current_state_first(app_env):
    app_env.traktor_state.apply("/deckLoaded/A", {"filePath": TRACK, "title": "Song"})

    class FakeRequest:
        async def is_disconnected(self):
            return True

    async def first_message():
        resp = await app_env.events(FakeRequest())
        gen = resp.body_iterator
        msg = await gen.__anext__()
        await gen.aclose()
        return msg

    msg = json.loads(asyncio.run(first_message()).removeprefix("data: "))
    assert msg["event"] == "traktor_state" and msg["state"]["decks"]["A"]["title"] == "Song"
