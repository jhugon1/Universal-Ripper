"""Offline API tests: temp DB and queue folder, stubbed YTMusic and yt-dlp, no network."""
import asyncio
import json
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40 + b"\xff\xd9"


class FakeYT:
    def search(self, query, filter=None, limit=None):
        return [{"videoId": "vid123", "title": "Fake Song", "artists": [{"name": "Fake Artist"}],
                 "album": {"name": "Fake Album"}, "thumbnails": [{"url": "https://example.invalid/small.jpg"},
                                                                  {"url": "https://example.invalid/big.jpg"}]}]


class FakeYDL:
    downloads = []
    tone = None        # a real 1 s mp3 that the "download" copies into place
    fail_times = 0     # make the next N download attempts fail
    gate = None        # a threading.Event: downloads wait for it (to test removing an item mid-download)

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        FakeYDL.downloads.append((self.opts.get("outtmpl"), list(urls)))
        if FakeYDL.gate is not None:
            FakeYDL.gate.wait(10)
        if FakeYDL.fail_times > 0:
            FakeYDL.fail_times -= 1
            raise RuntimeError("simulated network error")
        dest = Path(self.opts["outtmpl"].rsplit("/", 1)[0]) / "audio.mp3"
        shutil.copyfile(FakeYDL.tone, dest)


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("tone") / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-b:a", "128k", str(path)], check=True)
    return path


@pytest.fixture
def client(tmp_path, monkeypatch, tone):
    import app as app_module
    from ripper_queue import files as queue_files

    monkeypatch.chdir(ROOT)  # app mounts ./static and ./templates
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("RIPPER_QUEUE_DIR", str(tmp_path / "Ripper Queue"))
    monkeypatch.setattr(app_module, "get_ytmusic", lambda: FakeYT())
    monkeypatch.setattr(app_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(queue_files, "fetch_cover", lambda url: (JPEG, "image/jpeg"))
    FakeYDL.downloads.clear()
    FakeYDL.tone, FakeYDL.fail_times, FakeYDL.gate = tone, 0, None
    # `with` runs the lifespan: sets app.loop, creates the schema, starts the queue worker.
    with TestClient(app_module.app) as c:
        yield c, app_module


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def items_of(c):
    return c.get("/api/queue_items").json()["items"]


def first_item_with(c, status):
    return wait_for(lambda: next((i for i in items_of(c) if i["status"] == status), None))


def test_full_flow(client):
    c, _ = client
    r = c.post("/api/request", data={"artist": "x", "song": "y"})
    assert r.status_code == 200
    assert r.json()["matched_artist"] == "Fake Artist"
    assert r.json()["matched_song"] == "Fake Song"

    rows = c.get("/api/queue").json()
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    rid = rows[0]["id"]

    assert c.post("/api/update_status", data={"id": rid, "status": "approved"}).json() == {"status": "success"}
    assert c.get("/api/queue").json()[0]["status"] == "approved"
    assert items_of(c) == []  # only "selected" puts a request in the queue

    assert c.post("/api/delete_request", data={"id": rid}).json() == {"status": "success"}
    assert c.get("/api/queue").json() == []

    c.post("/api/request", data={"artist": "a", "song": "b"})
    c.post("/api/request", data={"artist": "c", "song": "d"})
    assert len(c.get("/api/queue").json()) == 2
    assert c.post("/api/clear_queue").json() == {"status": "success"}
    assert c.get("/api/queue").json() == []


def test_approved_request_downloads_into_the_session_folder(client, tmp_path):
    c, app_module = client
    q = asyncio.Queue()
    app_module.subscribers.add(q)
    try:
        c.post("/api/request", data={"artist": "x", "song": "y"})
        rid = c.get("/api/queue").json()[0]["id"]
        c.post("/api/update_status", data={"id": rid, "status": "selected"})
        c.post("/api/update_status", data={"id": rid, "status": "selected"})  # approving twice adds nothing

        item = first_item_with(c, "ready")
        state = c.get("/api/queue_items").json()
        assert len(state["items"]) == 1 and item["position"] == 1 and item["source"] == "request"
        folder = tmp_path / "Ripper Queue" / state["session"]["name"]
        path = folder / "001 Fake Artist - Fake Song.mp3"
        assert path.is_file() and list(folder.iterdir()) == [path]  # no .part or temp files left behind
        assert item["file_name"] == path.name and item["length_s"] == pytest.approx(1.0, abs=0.2)

        tags = ID3(path)
        assert (str(tags["TIT2"]), str(tags["TPE1"]), str(tags["TALB"])) == ("Fake Song", "Fake Artist", "Fake Album")
        assert tags["APIC:Cover"].data == JPEG
        assert FakeYDL.downloads[0][1] == ["https://music.youtube.com/watch?v=vid123"]

        assert c.get("/api/queue").json()[0]["status"] == "downloaded"  # the guest request is marked as before
        assert state["folder"]["reselect_needed"] is True
        assert "file_path" not in item and str(tmp_path) not in json.dumps(state)

        events = []
        wait_for(lambda: events.extend(
            json.loads(q.get_nowait().removeprefix("data: ")) for _ in range(q.qsize())) or
            any(e["event"] == "download_complete" for e in events))
        done = next(e for e in events if e["event"] == "download_complete")
        assert done["request_id"] == rid and done["status"] == "success"
    finally:
        app_module.subscribers.discard(q)


def test_dj_can_add_a_track_and_positions_follow_the_order_added(client):
    c, _ = client
    a = c.post("/api/queue_items/add", data={"artist": "A", "title": "One"}).json()
    b = c.post("/api/queue_items/add", data={"artist": "B", "title": "Two", "video_id": "vid123"}).json()
    assert (a["position"], b["position"]) == (1, 2) and a["source"] == "dj"
    wait_for(lambda: all(i["status"] == "ready" for i in items_of(c)))
    names = sorted(i["file_name"] for i in items_of(c))
    assert names == ["001 A - One.mp3", "002 B - Two.mp3"]


def test_a_failed_download_is_retried_once(client):
    c, _ = client
    FakeYDL.fail_times = 1
    c.post("/api/queue_items/add", data={"artist": "A", "title": "One"})
    first_item_with(c, "ready")
    assert len(FakeYDL.downloads) == 2  # first attempt failed, the retry worked


def test_two_failures_end_as_failed_with_a_short_message(client, tmp_path):
    c, _ = client
    FakeYDL.fail_times = 5
    c.post("/api/queue_items/add", data={"artist": "A", "title": "One"})
    item = first_item_with(c, "failed")
    assert len(FakeYDL.downloads) == 2 and "simulated network error" in item["error"]
    assert str(Path.home()) not in item["error"] and item["file_name"] is None
    assert not (tmp_path / "Ripper Queue").exists() or not any((tmp_path / "Ripper Queue").rglob("*.mp3"))


# ---- reorder, remove, retry, new night ---------------------------------------------------------
def post(c, path, **data):
    return c.post(f"/api/queue_items/{path}", data=data).json()


def add_ready(c, *names):
    for n in names:
        post(c, "add", artist=n, title=f"Song {n}")
    wait_for(lambda: len(items_of(c)) == len(names) and all(i["status"] == "ready" for i in items_of(c)))
    return items_of(c)


def folder_names(c, tmp_path):
    session = c.get("/api/queue_items").json()["session"]["name"]
    return sorted(p.name for p in (tmp_path / "Ripper Queue" / session).iterdir())


def set_status_in_db(tmp_path, item_id, status):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("UPDATE queue_items SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()
    conn.close()


def test_reordering_renames_the_files_and_flags_the_folder(client, tmp_path):
    c, _ = client
    a, b, d = add_ready(c, "A", "B", "D")
    post(c, "refreshed")
    assert c.get("/api/queue_items").json()["folder"]["reselect_needed"] is False

    state = post(c, "move", id=d["id"], direction="up")
    assert [i["artist"] for i in state["items"]] == ["A", "D", "B"]
    assert folder_names(c, tmp_path) == ["001 A - Song A.mp3", "002 D - Song D.mp3", "003 B - Song B.mp3"]
    assert state["folder"]["reselect_needed"] is True

    top = post(c, "move", id=a["id"], direction="up")  # already first: nothing changes
    assert [i["artist"] for i in top["items"]] == ["A", "D", "B"]
    assert post(c, "move", id=b["id"], direction="sideways")["items"] == top["items"]
    assert c.post("/api/queue_items/move", data={"id": 999, "direction": "up"}).status_code == 404


def test_loaded_and_played_items_keep_their_place_and_their_file_name(client, tmp_path):
    c, _ = client
    a, b, d = add_ready(c, "A", "B", "D")
    set_status_in_db(tmp_path, b["id"], "loaded")  # what the Traktor sync will do in 3a-ii
    state = post(c, "move", id=d["id"], direction="up")  # B is locked, so D swaps with A
    assert [(i["artist"], i["position"]) for i in state["items"]] == [("D", 1), ("B", 2), ("A", 3)]
    assert folder_names(c, tmp_path) == ["001 D - Song D.mp3", "002 B - Song B.mp3", "003 A - Song A.mp3"]
    assert next(i for i in state["items"] if i["artist"] == "B")["file_name"] == "002 B - Song B.mp3"
    set_status_in_db(tmp_path, a["id"], "played")
    assert post(c, "move", id=d["id"], direction="down")["items"][0]["artist"] == "D"  # nothing movable below it


def test_remove_never_touches_the_file(client, tmp_path):
    c, _ = client
    a, b = add_ready(c, "A", "B")
    before = folder_names(c, tmp_path)
    state = post(c, "remove", id=a["id"])
    assert [i["artist"] for i in state["items"]] == ["B"]
    assert folder_names(c, tmp_path) == before  # the file is still there for Traktor
    assert post(c, "remove", id=a["id"])["items"] == state["items"]  # removing twice is harmless
    assert c.post("/api/queue_items/remove", data={"id": 999}).status_code == 404


def test_removing_an_item_mid_download_leaves_no_file(client, tmp_path):
    c, _ = client
    FakeYDL.gate = threading.Event()
    post(c, "add", artist="A", title="Song A")
    wait_for(lambda: FakeYDL.downloads and first_item_with(c, "downloading"))
    post(c, "remove", id=items_of(c)[0]["id"])
    FakeYDL.gate.set()
    time.sleep(1.0)  # let the worker finish and discard the download
    root = tmp_path / "Ripper Queue"
    assert not root.exists() or not any(root.rglob("*.mp3")) and not any(root.rglob("*.part"))
    assert items_of(c) == []


def test_retry_puts_a_failed_item_back_in_the_queue(client):
    c, _ = client
    FakeYDL.fail_times = 5
    post(c, "add", artist="A", title="Song A")
    failed = first_item_with(c, "failed")
    FakeYDL.fail_times = 0
    assert post(c, "retry", id=failed["id"])["items"][0]["status"] in ("queued", "downloading", "ready")
    assert first_item_with(c, "ready")["error"] is None
    assert post(c, "retry", id=failed["id"])["items"][0]["status"] == "ready"  # only failed items are retried
    assert c.post("/api/queue_items/retry", data={"id": 999}).status_code == 404


def test_new_night_starts_a_new_folder_and_numbering(client, tmp_path):
    c, _ = client
    add_ready(c, "A")
    first = c.get("/api/queue_items").json()["session"]["name"]
    assert post(c, "new_night")["session"]["name"] == first + "-2"  # same day: suffix
    assert post(c, "new_night")["session"]["name"] == first + "-2"  # pressed again while still empty: no-op
    second_items = add_ready(c, "B")
    assert second_items[0]["position"] == 1 and [i["artist"] for i in items_of(c)] == ["B"]
    root = tmp_path / "Ripper Queue"
    assert sorted(p.name for p in root.iterdir()) == [first, first + "-2"]
    assert [p.name for p in (root / first).iterdir()] == ["001 A - Song A.mp3"]  # last night's file is untouched
    assert [p.name for p in (root / (first + "-2")).iterdir()] == ["001 B - Song B.mp3"]


# ---- Traktor events reach the queue ------------------------------------------------------------
@pytest.fixture
def traktor_port(monkeypatch):
    """Turn the app's Traktor receiver on, on a free port. Request this fixture BEFORE `client`."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", str(port))
    return port


def test_traktor_events_move_an_item_through_loaded_to_played(traktor_port, client, tmp_path):
    import httpx

    c, app_module = client
    http = httpx.Client(trust_env=False, timeout=2)
    base = f"http://127.0.0.1:{traktor_port}"
    post(c, "add", artist="A", title="Song A")
    post(c, "add", artist="B", title="Song B")
    wait_for(lambda: len(items_of(c)) == 2 and all(i["status"] == "ready" for i in items_of(c)))
    assert c.get("/api/queue_items").json()["up_next"]["artist"] == "A"
    conn = sqlite3.connect(tmp_path / "test.db")
    path = conn.execute("SELECT file_path FROM queue_items WHERE artist = 'A'").fetchone()[0]
    conn.close()

    def send(kind, body):
        for _ in range(50):  # the receiver starts in the background
            try:
                return http.post(f"{base}/{kind}", json=body)
            except httpx.TransportError:
                time.sleep(0.1)
        raise AssertionError("receiver did not start")

    def status_of(artist):
        return next(i["status"] for i in items_of(c) if i["artist"] == artist)

    send("deckLoaded/A", {"filePath": path.replace("\\", "/").upper(), "title": "x", "bpm": 126.4, "trackLength": 200.0})
    wait_for(lambda: status_of("A") == "loaded")
    state = c.get("/api/queue_items").json()
    assert next(i for i in state["items"] if i["artist"] == "A")["bpm"] == 126.4
    assert state["up_next"]["artist"] == "B"  # A is on a deck now

    app_module.traktor_state.decks["A"]["audible_seconds"] = 31.0  # what 31 s of playing would have counted
    send("updateDeck/A", {"isPlaying": True, "elapsedTime": 31.0})
    wait_for(lambda: status_of("A") == "played")
    send("deckLoaded/A", {"filePath": ""})  # ejected afterwards: still played
    time.sleep(0.6)
    assert status_of("A") == "played" and status_of("B") == "ready"


def test_printing_a_non_latin_title_cannot_break_the_app(monkeypatch):
    import io
    import sys

    import app as app_module

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)  # a typical Windows console
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", console)
    with pytest.raises(UnicodeEncodeError):
        print("日本語")  # without the fix this is what happened inside broadcast()
    app_module.make_console_safe()
    print("Broadcasting event:", {"query": "日本語 🎧"})
    assert b"Broadcasting event:" in raw.getvalue()


def test_download_direct_adds_to_tonights_queue_and_never_to_a_search_folder(client, tmp_path):
    c, app_module = client
    legacy = ROOT / "DJ_Search_Rips"
    before = sorted(p.name for p in legacy.iterdir()) if legacy.exists() else None
    answer = c.post("/api/download_direct", data={"query": "fake song fake artist"}).json()
    assert answer["status"] == "downloading" and answer["queued"] is True
    item = first_item_with(c, "ready")
    assert (item["artist"], item["title"]) == ("Fake Artist", "Fake Song")  # taken from the best match
    assert item["file_name"] == "001 Fake Artist - Fake Song.mp3" and item["source"] == "dj"
    session = c.get("/api/queue_items").json()["session"]["name"]
    assert (tmp_path / "Ripper Queue" / session / item["file_name"]).is_file()
    assert (sorted(p.name for p in legacy.iterdir()) if legacy.exists() else None) == before  # nothing new there
    assert not hasattr(app_module, "SEARCH_DIR") and not hasattr(app_module, "download_song")
