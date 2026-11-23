import os
import shutil
from pathlib import Path

import pytest

from ripper_queue import files, search

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("text", [
    f"https://www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/watch?v={VID}&list=PL123&t=42s",
    f"https://music.youtube.com/watch?v={VID}&si=abc",
    f"https://youtu.be/{VID}?si=abc",
    f"https://youtu.be/{VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://m.youtube.com/watch?v={VID}",
    f"  youtube.com/watch?v={VID}  ",
    f"music.youtube.com/watch?v={VID}",
])
def test_links_that_name_a_video(text):
    assert search.video_id_from_text(text) == VID


@pytest.mark.parametrize("text", [
    "", None, "Mississippi", "daft punk one more time", VID,  # a bare 11-character word is a search, not a video
    f"https://example.com/watch?v={VID}", f"https://notyoutube.com/{VID}", "https://www.youtube.com/",
    "https://www.youtube.com/watch?v=tooshort", "https://www.youtube.com/playlist?list=PL12345",
    "ftp://youtu.be/" + VID, "https://youtu.be/",
])
def test_things_that_are_not_a_video_link(text):
    assert search.video_id_from_text(text) is None


def test_search_results_are_normalised():
    raw = {"videoId": VID, "title": "T", "artists": [{"name": "A"}, {"name": "B"}, {"name": None}],
           "album": {"name": "LP"}, "duration": "1:02:03",
           "thumbnails": [{"url": "small"}, {"url": "big"}]}
    assert search.normalize_result(raw) == {"video_id": VID, "title": "T", "artist": "A, B", "album": "LP",
                                            "length_s": 3723, "thumbnail": "big"}
    bare = search.normalize_result({"videoId": VID})
    assert bare == {"video_id": VID, "title": "Untitled", "artist": "Unknown Artist", "album": None,
                    "length_s": None, "thumbnail": None}
    assert search.normalize_result({"title": "no id"}) is None
    assert search.normalize_result({"videoId": VID, "duration_seconds": 200, "duration": "9:99"})["length_s"] == 200


def test_a_looked_up_link_is_normalised_too():
    info = {"videoDetails": {"videoId": VID, "title": "T", "author": "Band - Topic", "lengthSeconds": "200",
                             "thumbnail": {"thumbnails": [{"url": "a"}, {"url": "b"}]}}}
    assert search.normalize_song(info) == {"video_id": VID, "title": "T", "artist": "Band", "album": None,
                                           "length_s": 200.0, "thumbnail": "b"}
    assert search.normalize_song({}) is None and search.normalize_song(None) is None
    assert search.normalize_song({"videoDetails": {"videoId": VID, "lengthSeconds": "x"}})["length_s"] is None


# ---- the link helper -----------------------------------------------------------------------------
def test_place_link_makes_a_hard_link(tone_mp3, tmp_path):
    source = tmp_path / "lib" / "Artist - Title.mp3"
    source.parent.mkdir()
    shutil.copy(tone_mp3, source)
    target, kind = files.place_link(source, tmp_path / "tonight", "001 Artist - Title.mp3")
    assert kind == "hardlink" and os.path.samefile(source, target) and target.parent.is_dir()
    assert os.stat(source).st_nlink == 2
    # a taken name is never overwritten
    again, _ = files.place_link(source, tmp_path / "tonight", "001 Artist - Title.mp3")
    assert again.name == "001 Artist - Title (2).mp3" and target.exists()


def test_place_link_copies_when_links_are_not_possible(tone_mp3, tmp_path, monkeypatch):
    source = tmp_path / "Artist - Title.mp3"
    shutil.copy(tone_mp3, source)
    monkeypatch.setattr(files.os, "link", lambda *a: (_ for _ in ()).throw(PermissionError("no links here")))
    target, kind = files.place_link(source, tmp_path / "tonight", "001 Artist - Title.mp3")
    assert kind == "copy" and target.read_bytes() == source.read_bytes() and not os.path.samefile(source, target)
    assert [p.name for p in (tmp_path / "tonight").iterdir()] == ["001 Artist - Title.mp3"]  # no .part left


def test_renaming_a_link_leaves_the_library_file_alone(tone_mp3, tmp_path):
    source = tmp_path / "Artist - Title.mp3"
    shutil.copy(tone_mp3, source)
    target, _ = files.place_link(source, tmp_path / "tonight", "001 Artist - Title.mp3")
    renamed = target.with_name("003 Artist - Title.mp3")
    os.rename(target, renamed)
    assert source.exists() and os.path.samefile(source, renamed) and not target.exists()


def test_error_text_has_no_terminal_colour_codes_and_no_home_folder():
    from ripper_queue import rip

    colored = RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] abc: This video is not available\n  second line")
    text = rip.clean_error(colored)
    assert text == "RuntimeError: ERROR: [youtube] abc: This video is not available second line"
    assert "\x1b" not in text
    assert str(Path.home()) not in rip.clean_error(OSError(f"cannot write {Path.home()}/x")) and len(rip.clean_error(OSError("x" * 900))) == 300


# ---- the extra folder: every rip is also linked into a folder the DJ picks ------------------------------
def test_link_extra_links_the_library_file_and_never_overwrites(tmp_path):
    import os
    from ripper_queue import rip, store
    conn = store.open_db(str(tmp_path / "t.db"))
    store.ensure_schema(conn)
    song = tmp_path / "Library" / "Artist - Title.mp3"
    song.parent.mkdir()
    song.write_bytes(b"audio")
    entry = {"path": str(song)}
    assert rip.link_extra(conn, entry) is None                      # nothing chosen: nothing happens
    extra = tmp_path / "House Set"
    extra.mkdir()
    store.meta_set(conn, rip.EXTRA_KEY, str(extra))
    first = rip.link_extra(conn, entry)
    assert first.name == "Artist - Title.mp3" and os.path.samefile(first, song)      # same file, no extra space
    second = rip.link_extra(conn, entry)                              # a second rip of it: kept, not overwritten
    assert second != first and first.exists() and song.read_bytes() == b"audio"
    store.meta_set(conn, rip.EXTRA_KEY, str(tmp_path / "gone"))
    assert rip.link_extra(conn, entry) is None                        # a folder that no longer exists is ignored


def test_extra_folder_endpoints_pick_remember_and_clear(tmp_path, monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "own.db"))
    folder = tmp_path / "My Tracks"
    folder.mkdir()
    monkeypatch.setattr(app_module, "choose_folder", lambda: str(folder))
    with TestClient(app_module.app) as c:
        assert c.get("/api/extra_folder").json() == {"set": False, "name": None}
        assert c.post("/api/extra_folder/pick").json() == {"set": True, "name": "My Tracks"}
        assert c.get("/api/extra_folder").json()["name"] == "My Tracks"            # remembered
        monkeypatch.setattr(app_module, "choose_folder", lambda: None)            # dialog cancelled: keeps the old one
        assert c.post("/api/extra_folder/pick").json()["name"] == "My Tracks"
        assert c.post("/api/extra_folder/clear").json() == {"set": False, "name": None}


def test_two_videos_with_the_same_name_finishing_together_never_overwrite_each_other(tmp_path, monkeypatch):
    import threading
    import time
    from ripper_queue import files, rip, store
    monkeypatch.setenv("RIPPER_LIBRARY_DIR", str(tmp_path / "Library"))
    real_free = files.free_path

    def slow_free(path):  # widen the window between choosing a name and writing the file
        chosen = real_free(path)
        time.sleep(0.15)
        return chosen

    monkeypatch.setattr(files, "free_path", slow_free)
    monkeypatch.setattr(files, "tag_file", lambda *a, **k: None)
    monkeypatch.setattr(files, "read_length", lambda p: 1.0)
    monkeypatch.setattr(files, "fetch_cover", lambda u: None)

    class Backend:
        def download(self, video_id, folder):
            out = folder / "x.mp3"
            out.write_bytes(video_id.encode())
            return out

    def one(video_id):
        conn = store.open_db(str(tmp_path / "t.db"))
        store.ensure_schema(conn)
        rip.ensure_in_library(conn, Backend(), video_id=video_id, artist="Band", title="Song")

    threads = [threading.Thread(target=one, args=(v,)) for v in ("versionAAAA", "versionBBBB")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    songs = sorted((tmp_path / "Library").glob("*.mp3"))
    assert [p.name for p in songs] == ["Band - Song (2).mp3", "Band - Song.mp3"]
    assert sorted(p.read_bytes() for p in songs) == [b"versionAAAA", b"versionBBBB"]   # both survive
