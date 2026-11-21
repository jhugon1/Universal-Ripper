"""Offline API tests: temp DB and queue folder, stubbed YTMusic and yt-dlp, no network."""
import asyncio
import json
import shutil
import subprocess
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

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        FakeYDL.downloads.append((self.opts.get("outtmpl"), list(urls)))
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
    monkeypatch.setattr(app_module, "SEARCH_DIR", str(tmp_path / "search"))
    FakeYDL.downloads.clear()
    FakeYDL.tone, FakeYDL.fail_times = tone, 0
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
