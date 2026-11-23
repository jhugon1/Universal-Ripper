import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from ripper_queue import files, store

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40 + b"\xff\xd9"


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("tone") / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-b:a", "128k", str(path)], check=True)
    return path


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "Ripper Queue"
    (root / "2026-11-21").mkdir(parents=True)
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)
    yield conn, root
    conn.close()


def make_item(env, tone, n, status="ready", position=None):
    """A queue item whose file really exists in the session folder, named with its position."""
    conn, root = env
    item, _ = store.add_item(conn, source="dj", artist=f"Artist {n}", title=f"Title {n}")
    path = root / "2026-11-21" / files.item_filename(item["position"], item["artist"], item["title"])
    shutil.copy(tone, path)
    store.update_item(conn, item["id"], status=status, file_path=str(path), marker_position=item["position"])
    if position is not None:
        store.update_item(conn, item["id"], position=position)
    return store.get_item(conn, item["id"])


# ---- names, images, tags ---------------------------------------------------------------------
def test_marker_names():
    assert files.item_filename(1, "Artist", "Title") == "001 Artist - Title.mp3"
    assert files.item_filename(12, "AC/DC", "Who?") == "012 ACDC - Who.mp3"
    assert files.marker_name(7, "001 Artist - Title.mp3") == "007 Artist - Title.mp3"
    assert files.marker_name(7, "Artist - Title.mp3") == "007 Artist - Title.mp3"
    assert files.strip_marker("1234 not a prefix.mp3") == "1234 not a prefix.mp3"
    assert files.marker_name(100, "099 a.mp3") == "100 a.mp3"  # three digits keep the order past 99


def test_free_path_adds_a_suffix(tmp_path):
    (tmp_path / "a.mp3").write_text("x")
    (tmp_path / "a (2).mp3").write_text("x")
    assert files.free_path(tmp_path / "b.mp3") == tmp_path / "b.mp3"
    assert files.free_path(tmp_path / "a.mp3") == tmp_path / "a (3).mp3"


def test_image_detection_and_cover_url():
    assert files.detect_image(JPEG) == "image/jpeg" and files.detect_image(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert files.detect_image(b"RIFF....WEBP") is None and files.detect_image(b"") is None
    assert files.cover_url("https://lh3.googleusercontent.com/abc=w60-h60-l90-rj").endswith("=w600-h600-l90-rj")
    assert files.cover_url("https://i.ytimg.com/vi/x/hqdefault.jpg") == "https://i.ytimg.com/vi/x/hqdefault.jpg"


def test_tagging_a_real_mp3(tone, tmp_path):
    path = tmp_path / "t.mp3"
    shutil.copy(tone, path)
    files.tag_file(path, title="Song", artist="Band", album="Album", cover=(JPEG, "image/jpeg"))
    tags = ID3(path)
    assert (str(tags["TIT2"]), str(tags["TPE1"]), str(tags["TALB"])) == ("Song", "Band", "Album")
    assert tags["APIC:Cover"].mime == "image/jpeg" and tags["APIC:Cover"].data == JPEG
    assert "TRCK" not in tags  # the order is in the file name, not the tags
    assert files.read_length(path) == pytest.approx(1.0, abs=0.15)
    files.tag_file(path, title="Song 2", artist="Band")  # retagging replaces, no leftovers
    again = ID3(path)
    assert str(again["TIT2"]) == "Song 2" and "TALB" not in again and "APIC:Cover" not in again


def test_length_of_a_non_audio_file_is_none(tmp_path):
    (tmp_path / "x.mp3").write_text("not audio")
    assert files.read_length(tmp_path / "x.mp3") is None


# ---- the order prefix and its safety rules ---------------------------------------------------
def test_apply_marker_renames_in_place(env, tone):
    conn, root = env
    a, b = make_item(env, tone, 1), make_item(env, tone, 2)
    folder = root / "2026-11-21"
    store.move_item(conn, b["id"], "up")
    assert files.apply_marker(conn, a["id"], root) and files.apply_marker(conn, b["id"], root)
    assert sorted(p.name for p in folder.iterdir()) == ["001 Artist 2 - Title 2.mp3", "002 Artist 1 - Title 1.mp3"]
    assert store.get_item(conn, b["id"])["file_path"] == str(folder / "001 Artist 2 - Title 2.mp3")
    assert store.get_item(conn, b["id"])["marker_position"] == 1


def test_locked_items_are_never_renamed(env, tone):
    conn, root = env
    for status in ("loaded", "played", "removed", "failed", "downloading"):
        item = make_item(env, tone, status, status=status)
        before = item["file_path"]
        store.update_item(conn, item["id"], position=99)
        assert files.apply_marker(conn, item["id"], root) is False
        assert store.get_item(conn, item["id"])["file_path"] == before
        assert Path(before).exists()


def test_a_file_held_by_another_program_is_retried_later(env, tone, monkeypatch):
    conn, root = env
    item = make_item(env, tone, 1, position=5)
    real_rename = files.os.rename
    monkeypatch.setattr(files.os, "rename", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))
    assert files.apply_marker(conn, item["id"], root) is False
    assert store.get_item(conn, item["id"])["marker_position"] == 1  # still the old prefix
    assert files.sync_markers(conn, root) == 0
    monkeypatch.setattr(files.os, "rename", real_rename)
    assert files.sync_markers(conn, root) == 1
    assert store.get_item(conn, item["id"])["marker_position"] == 5
    assert store.folder_state(conn)["reselect_needed"] is True


def test_paths_outside_the_queue_folder_are_left_alone(env, tone, tmp_path):
    conn, root = env
    outside = tmp_path / "elsewhere" / "001 Artist - Song.mp3"
    outside.parent.mkdir()
    shutil.copy(tone, outside)
    item, _ = store.add_item(conn, source="dj", artist="A", title="S")
    store.update_item(conn, item["id"], status="ready", file_path=str(outside), marker_position=1, position=9)
    assert files.apply_marker(conn, item["id"], root) is False
    assert outside.exists() and not list(outside.parent.glob("009*"))
    assert files.is_inside(root / "2026-11-21" / "x.mp3", root)
    assert not files.is_inside(root / ".." / "x.mp3", root) and not files.is_inside(outside, root)


def test_a_missing_file_is_skipped_not_created(env, tone):
    conn, root = env
    item = make_item(env, tone, 1, position=4)
    (root / "2026-11-21" / files.item_filename(1, "Artist 1", "Title 1")).unlink()
    assert files.apply_marker(conn, item["id"], root) is False
    assert list((root / "2026-11-21").iterdir()) == []


def test_nothing_is_ever_deleted_or_overwritten(env, tone):
    conn, root = env
    items = [make_item(env, tone, n) for n in (1, 2, 3)]
    folder = root / "2026-11-21"
    sizes = sorted(p.stat().st_size for p in folder.iterdir())
    store.move_item(conn, items[2]["id"], "up")
    store.move_item(conn, items[2]["id"], "up")
    for i in items:
        files.apply_marker(conn, i["id"], root)
    assert len(list(folder.iterdir())) == 3 and sorted(p.stat().st_size for p in folder.iterdir()) == sizes
    assert [p.name[:3] for p in sorted(folder.iterdir())] == ["001", "002", "003"]
