import os
import shutil

import pytest
from mutagen.id3 import ID3

from ripper_queue import files, library, store


@pytest.fixture
def env(tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)
    (tmp_path / "Library").mkdir()
    yield conn, tmp_path / "Library"
    conn.close()


def make(tone_mp3, folder, name, *, title=None, artist=None, video_id=None, album=None):
    path = folder / name
    shutil.copy(tone_mp3, path)
    if title:
        files.tag_file(path, title=title, artist=artist or "A", album=album, video_id=video_id)
    return path


def test_video_id_round_trips_through_the_tags(tone_mp3, tmp_path):
    path = tmp_path / "t.mp3"
    shutil.copy(tone_mp3, path)
    files.tag_file(path, title="Song", artist="Band", album="LP", video_id="abc123XYZ_-")
    assert str(ID3(path)["TXXX:YT_VIDEO_ID"]) == "abc123XYZ_-"
    tags = files.read_tags(path)
    assert (tags["title"], tags["artist"], tags["album"], tags["video_id"]) == ("Song", "Band", "LP", "abc123XYZ_-")
    assert tags["length_s"] == pytest.approx(1.0, abs=0.2)
    files.tag_file(path, title="Song", artist="Band")  # retagging without an id leaves none behind
    assert files.read_tags(path)["video_id"] is None


def test_an_untagged_file_falls_back_to_its_name(tone_mp3, tmp_path):
    path = tmp_path / "001 Some Artist - Some Title.mp3"
    shutil.copy(tone_mp3, path)
    tags = files.read_tags(path)
    assert (tags["artist"], tags["title"], tags["video_id"]) == ("Some Artist", "Some Title", None)
    plain = tmp_path / "Just A Name.mp3"
    shutil.copy(tone_mp3, plain)
    assert (files.read_tags(plain)["artist"], files.read_tags(plain)["title"]) == (None, "Just A Name")


def test_reindex_rebuilds_the_table_from_the_files(env, tone_mp3):
    conn, folder = env
    make(tone_mp3, folder, "A - One.mp3", title="One", artist="A", video_id="vid1")
    make(tone_mp3, folder, "B - Two.mp3", title="Two", artist="B", video_id="vid2")
    make(tone_mp3, folder, "Old - Untagged.mp3")
    assert library.reindex(conn, folder) == {"added": 3, "updated": 0, "removed": 0}
    assert library.find(conn, "vid1")["title"] == "One" and library.find(conn, "vid2")["artist"] == "B"
    assert library.known_ids(conn, ["vid1", "vid2", "nope", None]) == {"vid1", "vid2"}
    assert conn.execute("SELECT COUNT(*) FROM library WHERE video_id IS NULL").fetchone()[0] == 1  # the untagged one
    assert library.reindex(conn, folder) == {"added": 0, "updated": 0, "removed": 0}  # unchanged files are skipped


def test_reindex_notices_changes_and_forgets_missing_files_without_touching_any(env, tone_mp3):
    conn, folder = env
    one = make(tone_mp3, folder, "A - One.mp3", title="One", artist="A", video_id="vid1")
    two = make(tone_mp3, folder, "B - Two.mp3", title="Two", artist="B", video_id="vid2")
    library.reindex(conn, folder)
    files.tag_file(one, title="One (edited)", artist="A", video_id="vid1")
    os.utime(one, ns=(1, 5 * 10**18))
    two.unlink()
    counts = library.reindex(conn, folder)
    assert counts == {"added": 0, "updated": 1, "removed": 1}
    assert library.find(conn, "vid1")["title"] == "One (edited)" and library.find(conn, "vid2") is None
    assert one.exists()  # reindexing never deletes a file


def test_a_second_file_for_the_same_video_does_not_take_over_the_id(env, tone_mp3):
    conn, folder = env
    make(tone_mp3, folder, "A - One.mp3", title="One", artist="A", video_id="vid1")
    make(tone_mp3, folder, "A - One (2).mp3", title="One", artist="A", video_id="vid1")
    library.reindex(conn, folder)
    assert conn.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM library WHERE video_id = 'vid1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM library WHERE video_id IS NULL").fetchone()[0] == 1  # the other copy has no id


def test_find_forgets_an_entry_whose_file_was_deleted_by_hand(env, tone_mp3):
    conn, folder = env
    path = make(tone_mp3, folder, "A - One.mp3", title="One", artist="A", video_id="vid1")
    library.reindex(conn, folder)
    path.unlink()
    assert library.find(conn, "vid1") is None  # so it can be ripped again
    assert conn.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 0
    assert library.find(conn, None) is None and library.find(conn, "") is None


def test_add_updates_an_entry_for_the_same_path(env, tone_mp3):
    conn, folder = env
    path = make(tone_mp3, folder, "A - One.mp3", title="One", artist="A", video_id="vid1")
    first = library.add(conn, video_id="vid1", artist="A", title="One", album=None, length_s=1.0, path=path)
    again = library.add(conn, video_id="vid1", artist="A", title="One", album="LP", length_s=1.0, path=path)
    assert first["id"] == again["id"] and again["album"] == "LP"


# ---- the first-run copy of the old standalone rips -------------------------------------------
def test_old_rips_are_copied_not_moved_and_only_once(env, tone_mp3, tmp_path):
    conn, folder = env
    old = tmp_path / "old"
    old.mkdir()
    make(tone_mp3, old, "One More.mp3")
    make(tone_mp3, old, "Another.mp3")
    (old / "notes.txt").write_text("not audio")
    counts = library.ensure_ready(conn, old_dir=old)
    assert counts["migrated"] == 2 and counts["added"] == 2
    assert sorted(p.name for p in old.iterdir()) == ["Another.mp3", "One More.mp3", "notes.txt"]  # originals stay
    assert sorted(p.name for p in folder.iterdir()) == ["Another.mp3", "One More.mp3"]
    make(tone_mp3, old, "Later.mp3")
    assert library.ensure_ready(conn, old_dir=old)["migrated"] == 0  # first run only
    assert not (folder / "Later.mp3").exists()


def test_migration_never_overwrites_and_can_be_skipped(env, tone_mp3, tmp_path):
    conn, folder = env
    old = tmp_path / "old"
    old.mkdir()
    make(tone_mp3, old, "Same Name.mp3")
    mine = make(tone_mp3, folder, "Same Name.mp3", title="Mine", artist="Me")
    before = mine.read_bytes()
    assert library.migrate_old_rips(old, folder) == 0 and mine.read_bytes() == before
    conn2 = store.open_db(tmp_path / "other.db")
    store.ensure_schema(conn2)
    assert library.ensure_ready(conn2, old_dir=old, migrate=False)["migrated"] == 0  # dev mode skips it
    assert library.migrate_old_rips(tmp_path / "missing", folder) == 0
    conn2.close()


def test_library_folder_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPPER_LIBRARY_DIR", str(tmp_path / "x"))
    assert library.library_dir() == tmp_path / "x"
    monkeypatch.delenv("RIPPER_LIBRARY_DIR")
    assert library.library_dir().parts[-3:] == ("Music", "Traktor", "DJ_Search_Rips")
