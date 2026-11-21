import pytest

from ripper_queue import files, store, sync
from traktor.state import TraktorState

ROOT = "C:\\Music\\Ripper Queue\\2026-11-21\\"
NAME_A, NAME_B = "001 A - One.mp3", "002 B - Two.mp3"


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def st(clock):
    return TraktorState(clock=clock)


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)
    yield conn
    conn.close()


def ready_item(db, n, name, status="ready"):
    item, _ = store.add_item(db, source="dj", artist=f"Artist {n}", title=f"Title {n}")
    store.update_item(db, item["id"], status=status, file_path=ROOT + name, marker_position=item["position"])
    return store.get_item(db, item["id"])


def load(st, deck, path, bpm=124.0):
    st.apply(f"/deckLoaded/{deck}", {"filePath": path, "title": "T", "bpm": bpm, "trackLength": 300.0, "isPlaying": False})


def play_for(st, clock, deck, seconds, audible=True):
    if audible:
        st.apply(f"/updateChannel/{'ABCD'.index(deck) + 1}", {"isOnAir": True, "onAirLevel": 1})
    st.apply(f"/updateDeck/{deck}", {"isPlaying": True})
    for _ in range(int(seconds)):
        clock.t += 1
        st.apply(f"/updateDeck/{deck}", {"elapsedTime": clock.t})


def run(db, st, now=2000.0):
    changed = sync.apply_sync(db, st.decks, now)
    return changed, {i["id"]: i["status"] for i in db.execute("SELECT id, status FROM queue_items").fetchall()}


# ---- path matching ---------------------------------------------------------------------------
def test_paths_match_ignoring_case_and_slash_style():
    a = files.normalize_path("C:\\Music\\Ripper Queue\\2026-11-21\\001 A - One.mp3")
    assert a == files.normalize_path("c:/music/ripper queue/2026-11-21/001 a - one.mp3")
    assert a != files.normalize_path("C:\\Music\\Ripper Queue\\2026-11-21\\002 A - One.mp3")
    assert files.normalize_path(None) == "" and files.normalize_path("") == ""


def test_loading_the_file_marks_the_item_loaded(db, st):
    item = ready_item(db, 1, NAME_A)
    other = ready_item(db, 2, NAME_B)
    load(st, "A", (ROOT + NAME_A).lower().replace("\\", "/"))  # Traktor's spelling differs from ours
    changed, statuses = run(db, st)
    assert changed == [item["id"]] and statuses == {item["id"]: "loaded", other["id"]: "ready"}
    got = store.get_item(db, item["id"])
    assert got["loaded_at"] == 2000.0 and got["bpm"] == 124.0


def test_only_ready_items_with_files_take_part(db, st):
    for status in ("queued", "downloading", "failed", "removed", "played"):
        ready_item(db, status, f"{status}.mp3", status=status)
        load(st, "A", ROOT + f"{status}.mp3")
        assert run(db, st)[0] == []
    load(st, "B", ROOT + "unknown.mp3")  # a file we know nothing about is ignored
    assert run(db, st)[0] == []


# ---- the played rule -------------------------------------------------------------------------
def test_audible_for_30_seconds_marks_it_played(db, st, clock):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A)
    play_for(st, clock, "A", 29)
    assert run(db, st)[1][item["id"]] == "loaded"
    play_for(st, clock, "A", 2)
    changed, statuses = run(db, st, now=3000.0)
    assert statuses[item["id"]] == "played" and store.get_item(db, item["id"])["played_at"] == 3000.0


def test_playing_for_90_seconds_counts_even_if_the_channel_is_unknown_or_wrong(db, st, clock):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A)
    play_for(st, clock, "A", 89, audible=False)  # no channel data at all
    assert st.decks["A"]["audible_seconds"] == 0.0 and run(db, st)[1][item["id"]] == "loaded"
    play_for(st, clock, "A", 2, audible=False)
    assert run(db, st)[1][item["id"]] == "played"


def test_a_fader_down_deck_that_plays_briefly_is_not_played(db, st, clock):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A)
    st.apply("/updateChannel/1", {"isOnAir": True, "onAirLevel": 0.0})
    play_for(st, clock, "A", 60, audible=False)  # cueing in headphones for a minute
    assert run(db, st)[1][item["id"]] == "loaded"


def test_ejecting_an_unplayed_item_puts_it_back_but_played_is_final(db, st, clock):
    a, b = ready_item(db, 1, NAME_A), ready_item(db, 2, NAME_B)
    load(st, "A", ROOT + NAME_A)
    load(st, "B", ROOT + NAME_B)
    assert run(db, st)[1] == {a["id"]: "loaded", b["id"]: "loaded"}
    st.apply("/deckLoaded/A", {"filePath": ""})  # A is ejected without being played
    play_for(st, clock, "B", 31)
    changed, statuses = run(db, st)
    assert statuses == {a["id"]: "ready", b["id"]: "played"} and store.get_item(db, a["id"])["loaded_at"] is None
    st.apply("/deckLoaded/B", {"filePath": ""})
    assert run(db, st)[1][b["id"]] == "played"  # ejecting a played item changes nothing


def test_same_status_is_not_rewritten(db, st):
    ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A)
    assert len(run(db, st)[0]) == 1 and run(db, st)[0] == []  # nothing new the second time


def test_bpm_follows_what_traktor_reports_and_ignores_zero(db, st):
    item = ready_item(db, 1, NAME_A)
    load(st, "A", ROOT + NAME_A, bpm=0.0)
    run(db, st)
    assert store.get_item(db, item["id"])["bpm"] is None
    st.apply("/updateDeck/A", {"bpm": 127.5})  # analysis finished
    run(db, st)
    assert store.get_item(db, item["id"])["bpm"] == 127.5


# ---- Up Next ---------------------------------------------------------------------------------
def test_up_next_is_the_lowest_position_ready_item_of_tonight(db):
    session = store.current_session(db)
    assert store.up_next(db, session["id"]) is None
    a, b, c, d = (ready_item(db, n, f"{n}.mp3", status=s) for n, s in ((1, "played"), (2, "loaded"), (3, "ready"), (4, "ready")))
    assert store.up_next(db, session["id"])["id"] == c["id"]
    store.move_item(db, d["id"], "up")
    assert store.up_next(db, session["id"])["id"] == d["id"]
    store.update_item(db, d["id"], status="loaded")
    assert store.up_next(db, session["id"])["id"] == c["id"]
    store.new_night(db)
    assert store.up_next(db, store.current_session(db)["id"]) is None  # last night's ready items are not tonight's
