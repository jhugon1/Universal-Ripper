"""Restart recovery and stale-signal handling."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from traktor.state import LOST_AFTER, STALE_AFTER, TraktorState, deck_status

ROOT = Path(__file__).resolve().parent.parent
TRACK = r"C:\Music\Traktor\Ripper Queue\Artist - Song.mp3"


def loaded_state(now=1000.0):
    st = TraktorState(clock=lambda: now)
    st.apply("/deckLoaded/A", {"filePath": TRACK, "title": "Song", "artist": "Artist", "bpm": 120.0, "tempo": 1.02,
                               "trackLength": 200.0, "elapsedTime": 12.5, "isPlaying": True, "key": "9d"}, now)
    st.apply("/updateChannel/1", {"isOnAir": True, "onAirLevel": 1}, now)
    return st


# ---- staleness -------------------------------------------------------------------------------
def test_deck_status_thresholds():
    st = loaded_state(1000.0)
    a = st.decks["A"]
    assert deck_status(a, 1000.0) == "playing"
    assert deck_status(a, 1000.0 + STALE_AFTER) == "playing"
    assert deck_status(a, 1000.0 + STALE_AFTER + 0.1) == "stale"
    assert deck_status(a, 1000.0 + LOST_AFTER) == "stale"
    assert deck_status(a, 1000.0 + LOST_AFTER + 0.1) == "paused"  # shown as paused after 60 s of silence
    st.apply("/updateDeck/A", {"isPlaying": False}, 1010.0)
    assert deck_status(a, 1010.0) == "paused"
    assert deck_status(st.decks["B"], 1010.0) == "empty"


def test_a_fresh_event_clears_stale():
    st = loaded_state(1000.0)
    assert st.snapshot(now=1010.0)["decks"]["A"]["status"] == "stale"
    st.apply("/updateDeck/A", {"elapsedTime": 22.5}, 1010.0)
    assert st.snapshot(now=1010.5)["decks"]["A"]["status"] == "playing"


def test_duplicate_events_still_show_the_deck_is_alive():
    st = loaded_state(1000.0)
    assert st.apply("/updateDeck/A", {"elapsedTime": 12.5}, 1004.0) == []  # no change reported...
    assert st.decks["A"]["updated_at"] == 1004.0  # ...but the deck is not stale


def test_snapshot_carries_status_and_thresholds():
    snap = loaded_state().snapshot(now=1000.0)
    assert snap["stale_after"] == STALE_AFTER and snap["lost_after"] == LOST_AFTER
    assert snap["decks"]["A"]["status"] == "playing" and snap["decks"]["B"]["status"] == "empty"
    assert snap["decks"]["A"]["updated_at"] == 1000.0


# ---- restore ---------------------------------------------------------------------------------
def test_dump_load_round_trip_marks_decks_restored():
    old = loaded_state(1000.0)
    data = json.loads(json.dumps(old.dump()))  # must survive JSON
    fresh = TraktorState(clock=lambda: 1003.0)
    fresh.load(data)
    a = fresh.decks["A"]
    assert (a["file_path"], a["title"], a["tempo"], a["elapsed"], a["is_playing"]) == (TRACK, "Song", 1.02, 12.5, True)
    assert a["restored"] is True and fresh.decks["B"]["restored"] is False
    snap = fresh.snapshot(now=1003.0)
    assert snap["decks"]["A"]["restored"] is True and "file_path" not in snap["decks"]["A"]
    assert snap["decks"]["A"]["status"] == "playing"  # restored 3 s after its last event


def test_restored_playing_deck_goes_stale_then_paused_without_events():
    fresh = TraktorState()
    fresh.load(loaded_state(1000.0).dump())
    assert fresh.snapshot(now=1000.0 + STALE_AFTER + 1)["decks"]["A"]["status"] == "stale"
    assert fresh.snapshot(now=1000.0 + LOST_AFTER + 1)["decks"]["A"]["status"] == "paused"


def test_fresh_event_replaces_restored_data():
    fresh = TraktorState()
    fresh.load(loaded_state(1000.0).dump())
    fresh.apply("/updateDeck/A", {"elapsedTime": 40.0}, 1002.0)  # Traktor is still playing it
    a = fresh.decks["A"]
    assert a["restored"] is False and a["elapsed"] == 40.0 and a["title"] == "Song"
    fresh2 = TraktorState()
    fresh2.load(loaded_state(1000.0).dump())
    fresh2.apply("/deckLoaded/A", {"filePath": ""}, 1002.0)  # a different state in Traktor: the deck is empty
    assert fresh2.decks["A"]["file_path"] is None and fresh2.decks["A"]["restored"] is False


def test_load_ignores_garbage():
    st = TraktorState()
    st.load({"decks": {"A": {"file_path": 5, "tempo": "x", "elapsed": None, "is_playing": "yes"}, "Z": {}, "B": 7},
             "channels": {"1": {"is_on_air": "maybe", "on_air_level": "loud"}}, "master": 3, "last_played": "x"})
    assert st.decks["A"]["file_path"] is None and st.decks["A"]["tempo"] == 1.0 and st.decks["A"]["elapsed"] == 0.0
    assert st.decks["A"]["is_playing"] is False and st.channels["1"]["is_on_air"] is None
    assert st.snapshot()["now_playing"] is None


# ---- through the app and the database ---------------------------------------------------------
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(ROOT)
    db = tmp_path / "t.db"
    monkeypatch.setenv("RIPPER_DB", str(db))
    monkeypatch.setattr(app_module, "traktor_state", TraktorState())
    return app_module, db


def test_state_survives_an_app_restart(app_env):
    app_module, db = app_env
    with TestClient(app_module.app):
        app_module.traktor_state.apply("/deckLoaded/A", {"filePath": TRACK, "title": "Song", "trackLength": 200.0,
                                                         "elapsedTime": 30.0, "isPlaying": False})
    # the app has shut down and saved; a new process starts with an empty state
    app_module.traktor_state = TraktorState()
    with TestClient(app_module.app) as c:
        snap = c.get("/api/traktor/state").json()
    a = snap["decks"]["A"]
    assert a["title"] == "Song" and a["elapsed"] == 30.0 and a["restored"] is True
    assert "file_path" not in a and "C:" not in json.dumps(snap)


def test_old_or_corrupt_saved_state_is_ignored(app_env):
    app_module, db = app_env
    with TestClient(app_module.app):  # creates the schema
        pass
    good = json.dumps(loaded_state().dump())
    for saved_at, data in ((time.time() - 13 * 3600, good), (time.time(), "{not json"), (time.time(), "[]")):
        conn = sqlite3.connect(db)
        conn.execute("INSERT OR REPLACE INTO traktor_state (id, saved_at, data) VALUES (1, ?, ?)", (saved_at, data))
        conn.commit()
        conn.close()
        app_module.traktor_state = TraktorState()
        app_module.restore_traktor_state()
        assert app_module.traktor_state.decks["A"]["file_path"] is None, (saved_at, data[:20])


def test_state_reset_forgets_everything_but_keeps_the_object():
    st = loaded_state(1000.0)
    same = st
    st.reset()
    assert st is same and st.now_playing() is None and st.last_played is None and st.event_count == 0
    assert all(d["file_path"] is None and not d["is_playing"] for d in st.decks.values())
    assert all(c["is_on_air"] is None for c in st.channels.values())
    st.apply("/deckLoaded/A", {"filePath": TRACK, "title": "Again"}, 2000.0)  # still usable afterwards
    assert st.decks["A"]["title"] == "Again"


def test_reset_decks_clears_the_saved_copy_but_not_what_was_played(app_env):
    from ripper_queue import store

    app_module, db = app_env
    with TestClient(app_module.app) as c:
        app_module.traktor_state.apply("/deckLoaded/A", {"filePath": TRACK, "title": "Fake", "isPlaying": True})
        conn = store.open_db(db)
        item, _ = store.add_item(conn, source="dj", artist="A", title="T")
        store.update_item(conn, item["id"], status="played", file_path=TRACK)
        app_module.save_traktor_state()
        assert json.loads(conn.execute("SELECT data FROM traktor_state").fetchone()[0])["decks"]["A"]["title"] == "Fake"

        assert c.post("/api/traktor/reset").json() == {"status": "success"}
        assert c.get("/api/traktor/state").json()["decks"]["A"]["title"] is None
        saved = json.loads(conn.execute("SELECT data FROM traktor_state").fetchone()[0])
        assert saved["decks"]["A"]["file_path"] is None  # a restart would not bring the fake deck back
        assert store.get_item(conn, item["id"])["status"] == "played"  # played is final
        conn.close()
