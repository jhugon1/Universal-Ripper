import os
import shutil

import pytest

from ripper_queue import files, store, sync
from traktor.state import TraktorState

ROOT = "C:\\Music\\Ripper Queue\\2026-11-21\\"  # made-up paths: these tests only compare names
NAME_A, NAME_B = "001 A - One.mp3", "002 B - Two.mp3"


@pytest.fixture
def st():
    return TraktorState()


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)
    yield conn
    conn.close()


def ready_item(db, n, name, status="ready", path=None):
    item, _ = store.add_item(db, source="rip", artist=f"Artist {n}", title=f"Title {n}")
    store.update_item(db, item["id"], status=status, file_path=path or ROOT + name, marker_position=item["position"])
    return store.get_item(db, item["id"])


def load(st, deck, path, bpm=124.0, playing=False):
    st.apply(f"/deckLoaded/{deck}", {"filePath": path, "title": "T", "bpm": bpm, "trackLength": 300.0, "isPlaying": playing})


def run(db, st, now=2000.0):
    changed = sync.apply_sync(db, st.decks, now)
    return changed, {i["id"]: i["status"] for i in db.execute("SELECT id, status FROM queue_items").fetchall()}


# ---- loading means played --------------------------------------------------------------------
def test_paths_match_ignoring_case_and_slash_style():
    a = files.normalize_path("C:\\Music\\Ripper Queue\\2026-11-21\\001 A - One.mp3")
    assert a == files.normalize_path("c:/music/ripper queue/2026-11-21/001 a - one.mp3")
    assert a != files.normalize_path("C:\\Music\\Ripper Queue\\2026-11-21\\002 A - One.mp3")
    assert files.normalize_path(None) == "" and files.normalize_path("") == ""


def test_loading_a_track_even_once_marks_it_played(db, st):
    item, other = ready_item(db, 1, NAME_A), ready_item(db, 2, NAME_B)
    load(st, "A", (ROOT + NAME_A).lower().replace("\\", "/"), playing=False)  # just loaded, never played, Traktor's spelling
    changed, statuses = run(db, st)
    assert changed == [item["id"]] and statuses == {item["id"]: "played", other["id"]: "ready"}
    got = store.get_item(db, item["id"])
    assert got["played_at"] == 2000.0 and got["loaded_at"] == 2000.0 and got["bpm"] == 124.0


def test_there_is_no_time_rule_any_more(db, st):
    item = ready_item(db, 1, NAME_A)
    st.apply("/updateChannel/1", {"isOnAir": True, "onAirLevel": 0.0})  # fader down, deck paused: still counts
    load(st, "B", ROOT + NAME_A)
    assert run(db, st)[1][item["id"]] == "played"


def test_only_ready_items_take_part(db, st):
    for status in ("queued", "downloading", "failed", "removed"):
        ready_item(db, status, f"{status}.mp3", status=status)
        load(st, "A", ROOT + f"{status}.mp3")
        assert run(db, st)[0] == []
    load(st, "B", ROOT + "unknown.mp3")  # a file we know nothing about is ignored
    assert run(db, st)[0] == []
    no_file = ready_item(db, "nofile", "x.mp3")
    store.update_item(db, no_file["id"], file_path=None)
    assert run(db, st)[0] == []


def test_played_is_final_and_unloading_changes_nothing(db, st):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A)
    run(db, st)
    st.apply("/deckLoaded/A", {"filePath": ""})  # ejected
    assert run(db, st) == ([], {item["id"]: "played"})
    load(st, "A", ROOT + NAME_A)  # loaded again later
    assert run(db, st) == ([], {item["id"]: "played"}) and store.get_item(db, item["id"])["played_at"] == 2000.0


def test_the_deck_count_does_not_matter(db, st):
    a, b = ready_item(db, 1, NAME_A), ready_item(db, 2, NAME_B)
    load(st, "A", ROOT + NAME_A)
    load(st, "B", ROOT + NAME_B)
    assert run(db, st)[1] == {a["id"]: "played", b["id"]: "played"}


def test_bpm_follows_what_traktor_reports_and_ignores_zero(db, st):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A, bpm=0.0)
    run(db, st)
    assert store.get_item(db, item["id"])["bpm"] is None
    st.apply("/updateDeck/A", {"bpm": 127.5})  # analysis finished while it is still on the deck
    assert run(db, st)[0] == [item["id"]] and store.get_item(db, item["id"])["bpm"] == 127.5
    assert run(db, st)[0] == []  # nothing new the second time


# ---- the same file under another name (hard links) -------------------------------------------
def test_loading_the_library_copy_counts_because_it_is_the_same_file(db, st, tone_mp3, tmp_path):
    library = tmp_path / "Library" / "Artist 1 - Title 1.mp3"
    library.parent.mkdir()
    shutil.copy(tone_mp3, library)
    tonight, _ = files.place_link(library, tmp_path / "Ripper Queue" / "2026-11-22", "001 Artist 1 - Title 1.mp3")
    playlist, _ = files.place_link(library, tmp_path / "Playlist Rips" / "Mix (1h)", "03 Artist 1 - Title 1.mp3")
    unrelated = tmp_path / "Other.mp3"
    shutil.copy(tone_mp3, unrelated)
    item = ready_item(db, 1, "x", path=str(tonight))

    load(st, "A", str(unrelated))  # a different file: nothing
    assert run(db, st)[0] == []
    load(st, "A", str(library))  # the library copy: the same file as tonight's link
    assert run(db, st)[1] == {item["id"]: "played"}

    other = ready_item(db, 2, "y", path=str(tonight.parent / "002 Artist 1 - Title 1.mp3"))
    os.link(library, tonight.parent / "002 Artist 1 - Title 1.mp3")
    load(st, "B", str(playlist))  # a playlist copy loaded in Traktor
    assert run(db, st)[1][other["id"]] == "played"


def test_sync_never_touches_a_file(db, st, tone_mp3, tmp_path):
    folder = tmp_path / "Ripper Queue" / "2026-11-22"
    folder.mkdir(parents=True)
    path = folder / NAME_A
    shutil.copy(tone_mp3, path)
    ready_item(db, 1, "x", path=str(path))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    load(st, "A", str(path))
    run(db, st)
    st.apply("/deckLoaded/A", {"filePath": ""})
    run(db, st)
    assert [p.name for p in folder.iterdir()] == [NAME_A] and (path.read_bytes(), path.stat().st_mtime_ns) == before


# ---- the played list and what can still move -----------------------------------------------
def test_played_tracks_are_listed_in_the_order_they_were_loaded(db, st):
    a, b, c = (ready_item(db, n, f"{n}.mp3") for n in (1, 2, 3))
    session = store.current_session(db)
    load(st, "A", ROOT + "3.mp3")
    sync.apply_sync(db, st.decks, 100.0)
    load(st, "A", ROOT + "1.mp3")
    sync.apply_sync(db, st.decks, 200.0)
    queue, played = store.split_queue(store.list_items(db, session["id"]))
    assert [i["id"] for i in played] == [c["id"], a["id"]] and [i["id"] for i in queue] == [b["id"]]
    assert {i["id"] for i in queue}.isdisjoint(i["id"] for i in played)  # never in both


def test_a_played_track_keeps_its_place_when_the_rest_is_reordered(db, st):
    a, b, c = (ready_item(db, n, f"{n}.mp3") for n in (1, 2, 3))
    load(st, "A", ROOT + "2.mp3")
    run(db, st)
    items = store.list_items(db, store.current_session(db)["id"])
    assert store.plan_move(items, c["id"], "up") == {c["id"]: a["position"], a["id"]: c["position"]}  # skips the played one
    assert store.plan_move(items, b["id"], "up") == {} and store.plan_move(items, b["id"], "down") == {}
