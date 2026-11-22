import os
import shutil

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
