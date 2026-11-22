"""Offline API tests: temp DB, library and queue folders, stubbed YouTube Music and yt-dlp, no network."""
import asyncio
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3

ROOT = Path(__file__).resolve().parent.parent
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40 + b"\xff\xd9"
VID = "abcdefghij1"  # a video id that looks like a real one (11 characters)


class FakeYT:
    def search(self, query, filter=None, limit=None):
        return [{"videoId": VID, "title": "Fake Song", "artists": [{"name": "Fake Artist"}],
                 "album": {"name": "Fake Album"}, "duration": "3:20",
                 "thumbnails": [{"url": "https://example.invalid/small.jpg"}, {"url": "https://example.invalid/big.jpg"}]},
                {"title": "No video id: skipped"}]

    def get_song(self, video_id):
        return {"videoDetails": {"videoId": video_id, "title": "Linked Song", "author": "Linked Artist - Topic",
                                 "lengthSeconds": "200", "thumbnail": {"thumbnails": [{"url": "https://example.invalid/t.jpg"}]}}}


class FakeYDL:
    downloads = []     # (video id) per attempt
    tone = None        # a real 1 s mp3 that the "download" copies into place
    fail_times = 0     # make the next N download attempts fail
    gate = None        # a threading.Event: downloads wait for it (to remove an item mid-download)

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        FakeYDL.downloads.append(urls[0].split("v=")[1])
        if FakeYDL.gate is not None:
            FakeYDL.gate.wait(10)
        if FakeYDL.fail_times > 0:
            FakeYDL.fail_times -= 1
            raise RuntimeError("simulated network error")
        dest = Path(self.opts["outtmpl"].rsplit("/", 1)[0]) / "audio.mp3"
        shutil.copyfile(FakeYDL.tone, dest)


@pytest.fixture
def client(tmp_path, monkeypatch, tone_mp3):
    import app as app_module
    from ripper_queue import files as queue_files

    monkeypatch.chdir(ROOT)  # app mounts ./static and ./templates
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("RIPPER_QUEUE_DIR", str(tmp_path / "Ripper Queue"))  # the library is tmp_path/Library (conftest)
    monkeypatch.setattr(app_module, "get_ytmusic", lambda: FakeYT())
    monkeypatch.setattr(app_module.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(queue_files, "fetch_cover", lambda url: (JPEG, "image/jpeg"))
    FakeYDL.downloads.clear()
    FakeYDL.tone, FakeYDL.fail_times, FakeYDL.gate = tone_mp3, 0, None
    with TestClient(app_module.app) as c:  # the lifespan creates the schema and starts the worker
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


def rip(c, n, **extra):
    """Rip a fake track whose video id is derived from n."""
    data = {"video_id": f"vid{n:08d}", "artist": f"Artist {n}", "title": f"Song {n}", **extra}
    return c.post("/api/rip", data=data).json()


def post(c, path, **data):
    return c.post(f"/api/queue_items/{path}", data=data).json()


def rip_ready(c, *numbers):
    for n in numbers:
        rip(c, n)
    wait_for(lambda: len(items_of(c)) == len(numbers) and all(i["status"] == "ready" for i in items_of(c)))
    return items_of(c)


def library_names(tmp_path):
    folder = tmp_path / "Library"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def session_folder(c, tmp_path):
    return tmp_path / "Ripper Queue" / c.get("/api/queue_items").json()["session"]["name"]


def folder_names(c, tmp_path):
    return sorted(p.name for p in session_folder(c, tmp_path).iterdir())


# ---- search ------------------------------------------------------------------------------------
def test_search_results_say_what_is_already_in_the_library(client):
    c, _ = client
    results = c.get("/api/search", params={"q": "fake song"}).json()
    assert [r["video_id"] for r in results] == [VID]  # the result without a video id is dropped
    assert results[0] == {"video_id": VID, "title": "Fake Song", "artist": "Fake Artist", "album": "Fake Album",
                          "length_s": 200, "thumbnail": "https://example.invalid/big.jpg", "in_library": False}
    c.post("/api/rip", data={"video_id": VID, "artist": "Fake Artist", "title": "Fake Song"})
    first_item_with(c, "ready")
    assert c.get("/api/search", params={"q": "fake song"}).json()[0]["in_library"] is True


def test_a_pasted_link_finds_that_one_video(client):
    c, _ = client
    for link in (f"https://music.youtube.com/watch?v={VID}&si=x", f"https://youtu.be/{VID}", f"youtube.com/watch?v={VID}"):
        found = c.get("/api/search", params={"q": link}).json()
        assert len(found) == 1 and found[0]["video_id"] == VID and found[0]["artist"] == "Linked Artist"
    assert c.get("/api/search", params={"q": "https://example.com/watch?v=" + VID}).json()[0]["video_id"] == VID  # a search


# ---- a new rip ---------------------------------------------------------------------------------
def test_a_new_rip_goes_into_the_library_and_is_linked_into_tonights_folder(client, tmp_path):
    c, app_module = client
    q = asyncio.Queue()
    app_module.subscribers.add(q)
    try:
        answer = c.post("/api/rip", data={"video_id": VID, "artist": "Fake Artist", "title": "Fake Song",
                                          "album": "Fake Album", "thumbnail": "https://example.invalid/big.jpg"}).json()
        assert answer["kind"] == "new" and answer["item"]["position"] == 1 and answer["cover"] is None
        item = first_item_with(c, "ready")

        library_file = tmp_path / "Library" / "Fake Artist - Fake Song.mp3"
        link = session_folder(c, tmp_path) / "001 Fake Artist - Fake Song.mp3"
        assert library_file.is_file() and link.is_file() and item["file_name"] == link.name
        assert os.path.samefile(library_file, link)  # one file, two names
        assert list(session_folder(c, tmp_path).iterdir()) == [link]  # no .part or temp files left behind
        tags = ID3(library_file)
        assert (str(tags["TIT2"]), str(tags["TPE1"]), str(tags["TALB"])) == ("Fake Song", "Fake Artist", "Fake Album")
        assert str(tags["TXXX:YT_VIDEO_ID"]) == VID and tags["APIC:Cover"].data == JPEG
        assert item["length_s"] == pytest.approx(1.0, abs=0.2)
        assert FakeYDL.downloads == [VID]
        assert c.get("/api/queue_items").json()["folder"]["reselect_needed"] is True
        assert "file_path" not in item and str(tmp_path) not in json.dumps(c.get("/api/queue_items").json())

        events = []
        wait_for(lambda: events.extend(json.loads(q.get_nowait().removeprefix("data: ")) for _ in range(q.qsize()))
                 or any(e["event"] == "rip_result" for e in events))
        done = next(e for e in events if e["event"] == "rip_result")
        assert (done["kind"], done["ok"], done["video_id"], done["title"]) == ("new", True, VID, "Fake Song")
    finally:
        app_module.subscribers.discard(q)


def test_the_cover_comes_from_the_library_file(client):
    c, _ = client
    assert c.get(f"/api/library/cover/{VID}").status_code == 404  # not ripped yet
    c.post("/api/rip", data={"video_id": VID, "artist": "Fake Artist", "title": "Fake Song", "thumbnail": "https://x.invalid/c.jpg"})
    first_item_with(c, "ready")
    got = c.get(f"/api/library/cover/{VID}")
    assert got.status_code == 200 and got.content == JPEG and got.headers["content-type"] == "image/jpeg"


# ---- ripping something that is already there ---------------------------------------------------
def test_ripping_the_same_video_again(client, tmp_path):
    c, _ = client
    first = rip(c, 1)
    first_item_with(c, "ready")
    again = rip(c, 1)  # still in tonight's queue
    assert again["kind"] == "in_queue" and again["item"]["id"] == first["item"]["id"] and len(items_of(c)) == 1

    post(c, "remove", id=first["item"]["id"])  # out of the queue, but the rip is in the library for good
    third = rip(c, 1)
    assert third["kind"] == "duplicate" and third["item"]["status"] == "ready"  # linked at once, no waiting
    assert third["cover"] == "/api/library/cover/vid00000001"
    assert FakeYDL.downloads == ["vid00000001"]  # downloaded exactly once
    assert library_names(tmp_path) == ["Artist 1 - Song 1.mp3"]
    names = folder_names(c, tmp_path)
    assert names == ["001 Artist 1 - Song 1.mp3", "002 Artist 1 - Song 1.mp3"]
    assert os.path.samefile(tmp_path / "Library" / "Artist 1 - Song 1.mp3", session_folder(c, tmp_path) / names[1])


def test_a_different_version_is_a_different_rip(client, tmp_path):
    c, _ = client
    for vid in ("versionAAAA", "versionBBBB"):  # same artist and title, different videos
        c.post("/api/rip", data={"video_id": vid, "artist": "Band", "title": "Song"})
    wait_for(lambda: len(items_of(c)) == 2 and all(i["status"] == "ready" for i in items_of(c)))
    assert sorted(FakeYDL.downloads) == ["versionAAAA", "versionBBBB"]
    assert library_names(tmp_path) == ["Band - Song (2).mp3", "Band - Song.mp3"]


def test_without_hard_links_the_queue_folder_gets_a_copy(client, tmp_path, monkeypatch):
    from ripper_queue import files as queue_files

    c, _ = client
    monkeypatch.setattr(queue_files.os, "link", lambda *a: (_ for _ in ()).throw(OSError("links not supported")))
    rip(c, 1)
    item = first_item_with(c, "ready")
    library_file = tmp_path / "Library" / "Artist 1 - Song 1.mp3"
    link = session_folder(c, tmp_path) / item["file_name"]
    assert link.read_bytes() == library_file.read_bytes() and not os.path.samefile(library_file, link)
    assert [p.name for p in session_folder(c, tmp_path).iterdir()] == [item["file_name"]]  # no .part left


def test_removed_mid_download_keeps_the_rip_in_the_library_but_not_in_tonights_folder(client, tmp_path):
    c, _ = client
    FakeYDL.gate = threading.Event()
    answer = rip(c, 1)
    wait_for(lambda: FakeYDL.downloads and first_item_with(c, "downloading"))
    post(c, "remove", id=answer["item"]["id"])
    FakeYDL.gate.set()
    wait_for(lambda: library_names(tmp_path) == ["Artist 1 - Song 1.mp3"])
    time.sleep(0.5)
    assert items_of(c) == []
    folder = tmp_path / "Ripper Queue"
    assert not folder.exists() or not any(folder.rglob("*.mp3"))
    assert rip(c, 1)["kind"] == "duplicate"  # and now it can be linked without downloading again


# ---- failures ----------------------------------------------------------------------------------
def test_a_failed_download_is_retried_once(client):
    c, _ = client
    FakeYDL.fail_times = 1
    rip(c, 1)
    first_item_with(c, "ready")
    assert len(FakeYDL.downloads) == 2  # first attempt failed, the retry worked


def test_two_failures_end_as_failed_with_a_short_message(client, tmp_path):
    c, _ = client
    FakeYDL.fail_times = 5
    rip(c, 1)
    item = first_item_with(c, "failed")
    assert len(FakeYDL.downloads) == 2 and "simulated network error" in item["error"]
    assert str(Path.home()) not in item["error"] and item["file_name"] is None
    assert library_names(tmp_path) == []


def test_retry_puts_a_failed_item_back_in_the_queue(client):
    c, _ = client
    FakeYDL.fail_times = 5
    rip(c, 1)
    failed = first_item_with(c, "failed")
    FakeYDL.fail_times = 0
    assert post(c, "retry", id=failed["id"])["items"][0]["status"] in ("queued", "downloading", "ready")
    assert first_item_with(c, "ready")["error"] is None
    assert post(c, "retry", id=failed["id"])["items"][0]["status"] == "ready"  # only failed items are retried
    assert c.post("/api/queue_items/retry", data={"id": 999}).status_code == 404


# ---- reorder, remove, new night ----------------------------------------------------------------
def test_reordering_renames_tonights_links_and_never_the_library(client, tmp_path):
    c, _ = client
    a, b, d = rip_ready(c, 1, 2, 3)
    library_before = library_names(tmp_path)
    post(c, "refreshed")
    assert c.get("/api/queue_items").json()["folder"]["reselect_needed"] is False

    state = post(c, "move", id=d["id"], direction="up")
    assert [i["artist"] for i in state["items"]] == ["Artist 1", "Artist 3", "Artist 2"]
    assert folder_names(c, tmp_path) == ["001 Artist 1 - Song 1.mp3", "002 Artist 3 - Song 3.mp3", "003 Artist 2 - Song 2.mp3"]
    assert library_names(tmp_path) == library_before  # the library files are never renamed
    assert os.path.samefile(tmp_path / "Library" / "Artist 3 - Song 3.mp3", session_folder(c, tmp_path) / "002 Artist 3 - Song 3.mp3")
    assert state["folder"]["reselect_needed"] is True

    top = post(c, "move", id=a["id"], direction="up")  # already first: nothing changes
    assert [i["artist"] for i in top["items"]] == ["Artist 1", "Artist 3", "Artist 2"]
    assert post(c, "move", id=b["id"], direction="sideways")["items"] == top["items"]
    assert c.post("/api/queue_items/move", data={"id": 999, "direction": "up"}).status_code == 404


def test_remove_never_touches_any_file(client, tmp_path):
    c, _ = client
    a, b = rip_ready(c, 1, 2)
    before, library_before = folder_names(c, tmp_path), library_names(tmp_path)
    state = post(c, "remove", id=a["id"])
    assert [i["artist"] for i in state["items"]] == ["Artist 2"]
    assert folder_names(c, tmp_path) == before and library_names(tmp_path) == library_before
    assert post(c, "remove", id=a["id"])["items"] == state["items"]  # removing twice is harmless
    assert c.post("/api/queue_items/remove", data={"id": 999}).status_code == 404


def test_new_night_starts_a_new_folder_and_numbering(client, tmp_path):
    c, _ = client
    rip_ready(c, 1)
    first = c.get("/api/queue_items").json()["session"]["name"]
    assert post(c, "new_night")["session"]["name"] == first + "-2"  # same day: suffix
    assert post(c, "new_night")["session"]["name"] == first + "-2"  # pressed again while still empty: no-op
    rip(c, 2)
    second_items = wait_for(lambda: items_of(c) and all(i["status"] == "ready" for i in items_of(c)) and items_of(c))
    assert second_items[0]["position"] == 1 and [i["artist"] for i in items_of(c)] == ["Artist 2"]
    root = tmp_path / "Ripper Queue"
    assert sorted(p.name for p in root.iterdir()) == [first, first + "-2"]
    assert [p.name for p in (root / first).iterdir()] == ["001 Artist 1 - Song 1.mp3"]  # last night's file is untouched
    assert [p.name for p in (root / (first + "-2")).iterdir()] == ["001 Artist 2 - Song 2.mp3"]
    assert library_names(tmp_path) == ["Artist 1 - Song 1.mp3", "Artist 2 - Song 2.mp3"]


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
    print("Broadcasting event:", {"query": "日本語 \U0001f3a7"})
    assert b"Broadcasting event:" in raw.getvalue()
