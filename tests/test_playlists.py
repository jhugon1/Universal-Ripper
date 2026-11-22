import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ripper_queue import library, playlists, store

ROOT = Path(__file__).resolve().parent.parent


def apple_page(name="Mix", tracks=(), claimed=None):
    """A page shaped like Apple Music's: the playlist as JSON inside a script tag."""
    items = []
    for title, artist, ms, album in tracks:
        item = {"title": title, "artistName": artist, "duration": ms, "subtitleLinks": [{"title": artist}],
                "tertiaryLinks": [{"title": album}] if album else []}
        items.append(item)
    sections = [{"itemKind": "containerDetailHeaderLockup", "items": [{"title": name, "trackCount": claimed or len(items)}]},
                {"itemKind": "trackLockup", "items": items}, {"itemKind": "spacer", "items": [{}]}]
    payload = {"data": [{"data": {"sections": sections}}]}
    return f'<html><script type="application/json" id="serialized-server-data">{json.dumps(payload)}</script></html>'


TRACKS = [("DNA.", "Kendrick Lamar", 185947, "DAMN."), ("Alright", "Kendrick Lamar", 219333, "To Pimp a Butterfly"),
          ("Sicko Mode", "Travis Scott", 312820, "ASTROWORLD")]


# ---- naming ----------------------------------------------------------------------------------
@pytest.mark.parametrize("seconds,label", [(5460, "1h31m"), (22080, "6h08m"), (2700, "45m"), (59, "1m"), (29, "0m"),
                                            (3570, "1h00m"), (3599, "1h00m"), (None, "?"), (0, "?"), (-5, "?")])
def test_length_label(seconds, label):
    assert playlists.length_label(seconds) == label


def test_folder_and_file_names():
    assert playlists.folder_name("2010s Hip-Hop Essentials", 22080) == "2010s Hip-Hop Essentials (6h08m)"
    assert playlists.folder_name('Rock/Pop: "Best"?', 3600) == "RockPop Best (1h00m)"
    assert playlists.folder_name("   ", 60) == "Playlist (1m)" and playlists.folder_name("Mix", None) == "Mix (?)"
    assert playlists.track_filename(3, 40, "Artist", "Title") == "03 Artist - Title.mp3"
    assert playlists.track_filename(7, 100, "A", "T") == "007 A - T.mp3"  # wide enough for the biggest number
    assert playlists.track_filename(1, 12, "AC/DC", "Who Made Who?") == "01 ACDC - Who Made Who.mp3"


def test_which_service_a_link_belongs_to():
    assert playlists.service_of("https://music.apple.com/us/playlist/x/pl.abc") == "apple"
    assert playlists.service_of("https://open.spotify.com/playlist/2objzKylZ5fIYrdKYMkHHt?si=1") == "spotify"
    assert playlists.service_of("https://www.youtube.com/playlist?list=PL1") is None and playlists.service_of("") is None


def test_playlist_dir_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPPER_PLAYLIST_DIR", str(tmp_path / "x"))
    assert playlists.playlist_dir() == tmp_path / "x"
    monkeypatch.delenv("RIPPER_PLAYLIST_DIR")
    assert playlists.playlist_dir().parts[-3:] == ("Music", "Traktor", "Playlist Rips")


# ---- reading a playlist ----------------------------------------------------------------------
def test_apple_page_gives_name_order_durations_and_albums():
    parsed = playlists.parse_apple_page(apple_page("Hip-Hop", TRACKS))
    assert parsed["name"] == "Hip-Hop" and parsed["claimed_count"] == 3
    assert [(t["position"], t["title"]) for t in parsed["tracks"]] == [(1, "DNA."), (2, "Alright"), (3, "Sicko Mode")]
    assert parsed["tracks"][0] == {"position": 1, "artist": "Kendrick Lamar", "title": "DNA.", "album": "DAMN.", "duration_s": 185.947}


def test_fetch_computes_the_total_length_and_the_folder_name():
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body.encode()

    page = apple_page("Hip-Hop", TRACKS)
    result = playlists.fetch_playlist("https://music.apple.com/us/playlist/x/pl.abc", urlopen=lambda req, timeout=0: Resp(page))
    assert result["count"] == 3 and result["total_s"] == pytest.approx(718.1, abs=0.1)
    assert result["length_label"] == "12m" and result["folder_name"] == "Hip-Hop (12m)" and result["truncated"] is False


def test_a_page_with_fewer_tracks_than_the_playlist_has_is_flagged():
    parsed = playlists.parse_apple_page(apple_page("Big", TRACKS, claimed=250))
    assert playlists._finish("apple", parsed)["truncated"] is True
    unknown = [("A", "B", None, None)]
    assert playlists._finish("apple", playlists.parse_apple_page(apple_page("X", unknown)))["length_label"] == "?"


def test_unreadable_pages_and_links_give_a_clear_message():
    with pytest.raises(playlists.PlaylistError, match="format"):
        playlists.parse_apple_page("<html>nothing here</html>")
    with pytest.raises(playlists.PlaylistError, match="No tracks"):
        playlists.parse_apple_page(apple_page("Empty", []))
    with pytest.raises(playlists.PlaylistError, match="Spotify"):
        playlists.fetch_playlist("https://open.spotify.com/playlist/abc")
    with pytest.raises(playlists.PlaylistError, match="Paste"):
        playlists.fetch_playlist("https://example.com/list")


# ---- matching a track to a video -------------------------------------------------------------
def test_the_match_is_the_version_with_the_right_length():
    cands = [{"video_id": "radio", "length_s": 200}, {"video_id": "album", "length_s": 312}, {"video_id": "mix", "length_s": 540}]
    assert playlists.choose_match(cands, 311.5)["video_id"] == "album"
    assert playlists.choose_match(cands, 541)["video_id"] == "mix"
    assert playlists.choose_match(cands, 400)["video_id"] == "radio"  # nothing close: the first result
    assert playlists.choose_match(cands, None)["video_id"] == "radio" and playlists.choose_match([None], 10) is None
    assert playlists.choose_match([{"video_id": "x", "length_s": None}], 100)["video_id"] == "x"


# ---- ripping a playlist ----------------------------------------------------------------------
class FakeYT:
    """Search results by query; every track resolves to a video id derived from its title."""
    fail_titles = set()

    def search(self, query, filter=None, limit=None):
        title = query.split(" ", 2)[-1] if False else query
        for name in self.fail_titles:
            if name in query:
                return []
        vid = "v" + str(abs(hash(query)) % 10**9).zfill(10)
        artist, _, rest = query.partition(" ")
        return [{"videoId": vid, "title": rest or query, "artists": [{"name": artist}], "duration_seconds": 200,
                 "thumbnails": []}]


class FakeBackend:
    def __init__(self, tone):
        self.tone, self.downloads, self.fail = tone, [], set()

    def download(self, video_id, dest_dir):
        self.downloads.append(video_id)
        if video_id in self.fail:
            raise RuntimeError("simulated download error")
        out = Path(dest_dir) / "audio.mp3"
        shutil.copyfile(self.tone, out)
        return out


@pytest.fixture
def env(tmp_path, tone_mp3):
    db = tmp_path / "q.db"
    conn = store.open_db(db)
    store.ensure_schema(conn)
    FakeYT.fail_titles = set()
    yield conn, db, FakeBackend(tone_mp3), tmp_path
    conn.close()


def result_for(name="Mix", tracks=TRACKS):
    return playlists._finish("apple", playlists.parse_apple_page(apple_page(name, tracks)))


def rip(env, result=None, **kw):
    conn, db, backend, tmp = env
    job = playlists.PlaylistJob(result or result_for(), root=tmp / "Playlist Rips")
    playlists.run_job(job, db_path=db, backend=backend, yt_factory=FakeYT, **kw)
    return job


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_a_playlist_is_ripped_in_order_into_its_own_folder(env):
    conn, db, backend, tmp = env
    job = rip(env)
    assert job.folder == tmp / "Playlist Rips" / "Mix (12m)" and job.status == "done"
    assert (job.done, job.failed, job.total) == (3, 0, 3)
    assert names(job.folder) == ["01 Kendrick Lamar - DNA.mp3", "02 Kendrick Lamar - Alright.mp3", "03 Travis Scott - Sicko Mode.mp3"]
    assert len(backend.downloads) == 3 and len(os.listdir(tmp / "Library")) == 3
    for link in job.folder.iterdir():  # the playlist folder holds links to the library files
        assert any(os.path.samefile(link, lib) for lib in (tmp / "Library").iterdir())
    assert conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0] == 0  # never in tonight's queue


def test_a_track_already_in_the_library_is_linked_not_downloaded(env):
    conn, db, backend, tmp = env
    first = rip(env, result_for("One", TRACKS[:1]))
    assert len(backend.downloads) == 1
    again = rip(env, result_for("Another", TRACKS[:2]))  # shares DNA. with the first playlist
    assert len(backend.downloads) == 2  # only Alright was new
    assert (again.done, again.failed) == (2, 0)
    assert len(os.listdir(tmp / "Library")) == 2
    assert os.path.samefile(next(first.folder.iterdir()), next(p for p in again.folder.iterdir() if "DNA" in p.name))


def test_failures_are_counted_and_do_not_stop_the_playlist(env):
    conn, db, backend, tmp = env
    FakeYT.fail_titles = {"Alright"}  # no match on YouTube Music
    job = rip(env)
    assert (job.done, job.failed) == (2, 1) and "no match" in job.problems[0] and "Alright" in job.problems[0]
    assert names(job.folder) == ["01 Kendrick Lamar - DNA.mp3", "03 Travis Scott - Sicko Mode.mp3"]  # numbers keep their places
    backend.fail = {v for v in [None]}
    job2 = rip(env, result_for("Broken", TRACKS[:1]))
    assert job2.done == 1


def test_a_download_error_on_one_track_is_a_failure_not_a_crash(env):
    conn, db, backend, tmp = env
    backend.download = lambda vid, dest: (_ for _ in ()).throw(RuntimeError("simulated download error"))
    job = rip(env, result_for("Nope", TRACKS[:2]))
    assert (job.done, job.failed) == (0, 2) and "simulated download error" in job.problems[0] and job.status == "done"
    assert not any(job.folder.iterdir())


def test_ripping_again_updates_the_folder_in_place(env):
    conn, db, backend, tmp = env
    job = rip(env)
    kept = job.folder / "01 Kendrick Lamar - DNA.mp3"
    kept_stat = kept.stat().st_ino
    (job.folder / "02 Kendrick Lamar - Alright.mp3").unlink()  # a track is missing now
    extra = job.folder / "notes.txt"
    extra.write_text("mine")
    before = len(backend.downloads)
    again = rip(env)
    assert again.folder == job.folder  # same name and length: the same folder
    assert names(job.folder) == ["01 Kendrick Lamar - DNA.mp3", "02 Kendrick Lamar - Alright.mp3", "03 Travis Scott - Sicko Mode.mp3", "notes.txt"]
    assert kept.stat().st_ino == kept_stat and extra.read_text() == "mine"  # existing files untouched
    assert len(backend.downloads) == before  # the missing track came from the library: no download
    assert (again.done, again.linked) == (3, 1)  # three present, one newly linked


def test_a_different_total_length_is_a_new_folder(env):
    conn, db, backend, tmp = env
    short = rip(env, result_for("Mix", TRACKS[:2]))
    longer = rip(env, result_for("Mix", TRACKS))
    assert short.folder != longer.folder and short.folder.name == "Mix (7m)" and longer.folder.name == "Mix (12m)"
    assert len(names(short.folder)) == 2 and len(names(longer.folder)) == 3


def test_a_big_playlist_numbers_keep_their_width(env):
    conn, db, backend, tmp = env
    many = [(f"Song {i}", "Band", 200000, None) for i in range(1, 121)]
    job = rip(env, result_for("Big", many), limit=3)  # only the first three are ripped
    assert job.total == 3 and names(job.folder) == ["001 Band - Song 1.mp3", "002 Band - Song 2.mp3", "003 Band - Song 3.mp3"]


def test_progress_is_reported(env):
    conn, db, backend, tmp = env
    seen = []
    rip(env, on_progress=lambda j: seen.append((j.done + j.failed, j.status)))
    assert seen[-1] == (3, "done") and [n for n, _ in seen] == sorted(n for n, _ in seen)


def test_the_same_video_is_never_downloaded_twice_at_once(env):
    conn, db, backend, tmp = env
    same = [("Song", "Band", 200000, None), ("Song", "Band", 200000, None)]  # two tracks that resolve to one video
    job = rip(env, result_for("Twice", same), concurrency=2)
    assert job.done == 2 and len(backend.downloads) == 1 and len(os.listdir(tmp / "Library")) == 1


# ---- through the API -------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch, tone_mp3):
    import app as app_module

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RIPPER_QUEUE_DIR", str(tmp_path / "Ripper Queue"))
    backend = FakeBackend(tone_mp3)
    FakeYT.fail_titles = set()
    monkeypatch.setattr(app_module, "get_ytmusic", lambda: FakeYT())
    monkeypatch.setattr(app_module, "YtBackend", lambda: backend)
    monkeypatch.setattr(playlists, "fetch_playlist", lambda url: result_for("Hip-Hop", TRACKS))
    with TestClient(app_module.app) as c:
        yield c, app_module, backend


def test_fetch_then_rip_through_the_api(client, tmp_path):
    c, app_module, backend = client
    q = asyncio.Queue()
    app_module.subscribers.add(q)
    try:
        fetched = c.post("/api/playlist/fetch", data={"url": "https://music.apple.com/us/playlist/x/pl.abc"}).json()
        assert (fetched["name"], fetched["count"], fetched["length_label"]) == ("Hip-Hop", 3, "12m")
        assert fetched["dest"] == str(tmp_path / "Playlist Rips" / "Hip-Hop (12m)") and fetched["existing"] == 0
        assert [t["title"] for t in fetched["tracks"]] == ["DNA.", "Alright", "Sicko Mode"] and "album" not in fetched["tracks"][0]

        started = c.post("/api/playlist/rip", data={"fetch_id": fetched["fetch_id"]}).json()
        assert started["status"] in ("running", "done") and started["total"] == 3
        deadline = time.time() + 15
        events = []
        while time.time() < deadline and not any(e["event"] == "playlist_done" for e in events):
            while not q.empty():
                events.append(json.loads(q.get_nowait().removeprefix("data: ")))
            time.sleep(0.05)
        done = next(e for e in events if e["event"] == "playlist_done")
        assert (done["done"], done["failed"], done["total"], done["name"]) == (3, 0, 3, "Hip-Hop")
        assert c.get("/api/playlist/job").json()["status"] == "done"
        folder = tmp_path / "Playlist Rips" / "Hip-Hop (12m)"
        assert len(names(folder)) == 3
        assert c.get("/api/queue_items").json()["items"] == []  # not in tonight's queue
        again = c.post("/api/playlist/fetch", data={"url": "https://music.apple.com/x"}).json()
        assert again["existing"] == 3  # the page can say the folder is already there
    finally:
        app_module.subscribers.discard(q)


def test_api_errors_are_clear(client, monkeypatch):
    c, app_module, backend = client
    assert c.post("/api/playlist/rip", data={"fetch_id": "nope"}).status_code == 404
    monkeypatch.setattr(playlists, "fetch_playlist", lambda url: (_ for _ in ()).throw(playlists.PlaylistError("Paste an Apple Music or Spotify playlist link.")))
    bad = c.post("/api/playlist/fetch", data={"url": "https://example.com"})
    assert bad.status_code == 400 and "Paste" in bad.json()["message"]
    assert c.get("/api/playlist/job").json() == {"status": "idle"}


def test_only_one_playlist_is_ripped_at_a_time(client):
    c, app_module, backend = client
    import threading

    gate = threading.Event()
    real = backend.download
    backend.download = lambda vid, dest: (gate.wait(10), real(vid, dest))[1]
    fetched = c.post("/api/playlist/fetch", data={"url": "https://music.apple.com/x"}).json()
    assert c.post("/api/playlist/rip", data={"fetch_id": fetched["fetch_id"]}).status_code == 200
    second = c.post("/api/playlist/rip", data={"fetch_id": fetched["fetch_id"]})
    assert second.status_code == 409 and "already" in second.json()["message"]
    gate.set()
    deadline = time.time() + 15
    while time.time() < deadline and c.get("/api/playlist/job").json()["status"] != "done":
        time.sleep(0.1)
