"""Offline smoke tests: temp DB, stubbed YTMusic and yt-dlp, no network."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


class FakeYT:
    def search(self, query, filter=None, limit=None):
        return [{"videoId": "vid123", "title": "Fake Song",
                 "artists": [{"name": "Fake Artist"}]}]


class FakeYDL:
    downloads = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        FakeYDL.downloads.append((self.opts.get("outtmpl"), list(urls)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(ROOT)  # app mounts ./static and ./templates
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "test.db"))
    monkeypatch.setattr(app_module, "get_ytmusic", lambda: FakeYT())
    monkeypatch.setattr(app_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(app_module, "SEARCH_DIR", str(tmp_path / "search"))
    monkeypatch.setattr(app_module, "QUEUE_DIR", str(tmp_path / "queue"))
    FakeYDL.downloads.clear()
    # `with` runs the lifespan: sets app.loop and creates the schema.
    with TestClient(app_module.app) as c:
        yield c, app_module


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

    assert c.post("/api/delete_request", data={"id": rid}).json() == {"status": "success"}
    assert c.get("/api/queue").json() == []

    c.post("/api/request", data={"artist": "a", "song": "b"})
    c.post("/api/request", data={"artist": "c", "song": "d"})
    assert len(c.get("/api/queue").json()) == 2
    assert c.post("/api/clear_queue").json() == {"status": "success"}
    assert c.get("/api/queue").json() == []


def test_selected_triggers_download_and_event(client):
    c, app_module = client
    q = asyncio.Queue()
    app_module.subscribers.add(q)
    try:
        c.post("/api/request", data={"artist": "x", "song": "y"})
        rid = c.get("/api/queue").json()[0]["id"]
        c.post("/api/update_status", data={"id": rid, "status": "selected"})

        assert len(FakeYDL.downloads) == 1
        assert FakeYDL.downloads[0][0].endswith("/Fake Artist - Fake Song.%(ext)s")
        assert FakeYDL.downloads[0][1] == ["https://music.youtube.com/watch?v=vid123"]
        assert c.get("/api/queue").json()[0]["status"] == "downloaded"

        msg = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                msg = q.get_nowait()
                break
            except asyncio.QueueEmpty:
                time.sleep(0.05)
        assert msg is not None, "no SSE event received"
        event = json.loads(msg.removeprefix("data: "))
        assert event["event"] == "download_complete"
        assert event["request_id"] == rid
    finally:
        app_module.subscribers.discard(q)
