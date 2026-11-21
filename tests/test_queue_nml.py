import os

import pytest

from ripper_queue import files, nml, store

ROOT = "C:\\Music\\Ripper Queue\\2026-11-21\\"

NML = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<NML VERSION="20"><HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>
<COLLECTION ENTRIES="4">
<ENTRY TITLE="One"><LOCATION DIR="/:Music/:Ripper Queue/:2026-11-21/:" FILE="001 A - One.mp3" VOLUME="C:"></LOCATION>
  <TEMPO BPM="128.000504" BPM_QUALITY="100.000000"></TEMPO></ENTRY>
<ENTRY TITLE="Two"><LOCATION DIR="/:Music/:Ripper Queue/:2026-11-21/:" FILE="002 B - Two.mp3" VOLUME="C:"></LOCATION>
  <TEMPO BPM="0.000000"></TEMPO></ENTRY>
<ENTRY TITLE="Three"><LOCATION DIR="/:Music/:Ripper Queue/:2026-11-21/:" FILE="003 C - Three.mp3" VOLUME="C:"></LOCATION></ENTRY>
<ENTRY TITLE="Four"><LOCATION DIR="/:Music/:" FILE="Elsewhere.mp3" VOLUME="C:"></LOCATION><TEMPO BPM="90"></TEMPO></ENTRY>
</COLLECTION></NML>
"""


@pytest.fixture
def collection(tmp_path):
    path = tmp_path / "collection.nml"
    path.write_text(NML, encoding="utf-8")
    return path


def test_path_rebuilt_from_a_location():
    assert nml.entry_path("C:", "/:Users/:me/:Music/:", "a.mp3") == "C:\\Users\\me\\Music\\a.mp3"
    assert nml.entry_path("Macintosh HD", "/:Users/:me/:", "a.mp3") == "/Users/me/a.mp3"
    assert nml.entry_path(None, None, None) == ""


def test_index_has_analyzed_tracks_only(collection):
    index = nml.bpm_index(collection)
    assert index == {files.normalize_path("C:\\Music\\Ripper Queue\\2026-11-21\\001 A - One.mp3"): pytest.approx(128.000504),
                     files.normalize_path("C:\\Music\\Elsewhere.mp3"): 90.0}  # BPM 0 and no TEMPO are skipped


def test_index_is_read_only_and_cached_until_the_file_changes(collection):
    before = (collection.read_bytes(), collection.stat().st_mtime_ns)
    first = nml.bpm_index(collection)
    assert nml.bpm_index(collection) is first  # same object: served from the cache
    assert (collection.read_bytes(), collection.stat().st_mtime_ns) == before  # never written to
    collection.write_text(NML.replace("128.000504", "130"), encoding="utf-8")
    os.utime(collection, ns=(1, 2 * 10**18))  # make the change visible whatever the clock resolution
    assert nml.bpm_index(collection)[files.normalize_path(ROOT + "001 A - One.mp3")] == 130.0


def test_an_unreadable_or_half_written_file_gives_nothing_and_is_retried(collection):
    good = collection.read_text(encoding="utf-8")
    collection.write_text(good[: len(good) // 2], encoding="utf-8")  # Traktor mid-write
    os.utime(collection, ns=(1, 3 * 10**18))
    assert nml.bpm_index(collection) == {}
    collection.write_text(good, encoding="utf-8")
    os.utime(collection, ns=(1, 4 * 10**18))
    assert nml.bpm_index(collection) != {}
    assert nml.bpm_index(collection.parent / "missing.nml") == {}


def test_newest_traktor_folder_is_chosen(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPER_TRAKTOR_DIR", raising=False)
    base = tmp_path / "Documents" / "Native Instruments"
    for name in ("Traktor 3.11.1", "Traktor 4.0.2", "Traktor 4.10.0", "Traktor Backup"):
        (base / name).mkdir(parents=True)
        (base / name / "collection.nml").write_text("<NML/>")
    (base / "Traktor 5.0.0").mkdir()  # no collection: not a candidate
    monkeypatch.setattr(nml.Path, "home", classmethod(lambda cls: tmp_path))
    assert nml.traktor_dir().name == "Traktor 4.10.0" and nml.collection_path().name == "collection.nml"
    monkeypatch.setenv("RIPPER_TRAKTOR_DIR", str(base / "Traktor 3.11.1"))
    assert nml.traktor_dir().name == "Traktor 3.11.1"


def test_fill_bpm_only_fills_gaps(collection, tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)

    def item(n, name, status="ready", bpm=None):
        it, _ = store.add_item(conn, source="dj", artist=f"A{n}", title=f"T{n}")
        store.update_item(conn, it["id"], status=status, file_path=ROOT + name, bpm=bpm)
        return it["id"]

    a = item(1, "001 A - One.mp3")
    live = item(2, "001 A - One.mp3", status="loaded", bpm=127.0)   # Traktor's live value wins: not overwritten
    gone = item(3, "001 A - One.mp3", status="removed")             # removed items are ignored
    unknown = item(4, "009 Z - Nowhere.mp3")
    assert nml.fill_bpm(conn, nml.bpm_index(collection)) == 1
    bpm = {i: store.get_item(conn, i)["bpm"] for i in (a, live, gone, unknown)}
    assert bpm == {a: 128.0, live: 127.0, gone: None, unknown: None}
    assert nml.fill_bpm(conn, nml.bpm_index(collection)) == 0
    conn.close()
