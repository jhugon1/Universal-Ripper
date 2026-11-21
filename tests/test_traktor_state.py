import json
from pathlib import Path

import pytest

from traktor.state import TraktorState

SAMPLES = Path(__file__).resolve().parent.parent / "fixtures" / "traktor" / "samples"
TRACK_A = r"C:\Music\Traktor\Ripper Queue\Artist A - Song A.mp3"
TRACK_B = r"C:\Music\Traktor\Ripper Queue\Artist B - Song B.mp3"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def st(clock):
    return TraktorState(clock=clock)


def loaded(path=TRACK_A, **kw):
    body = {"filePath": path, "title": "Song", "artist": "Artist", "bpm": 120.0, "tempo": 1, "key": "9d",
            "trackLength": 200.0, "elapsedTime": 0, "isPlaying": False}
    body.update(kw)
    return body


def startup(st):
    for d in "ABCD":
        st.apply(f"/deckLoaded/{d}", {"filePath": "", "title": "", "bpm": 120, "tempo": 1, "trackLength": 0})
    for c in "1234":
        st.apply(f"/updateChannel/{c}", {"isOnAir": True})
        st.apply(f"/updateChannel/{c}", {"onAirLevel": 1})
    st.apply("/updateMasterClock", {"deck": None, "bpm": 120})


def play(st, deck="A", path=TRACK_A):
    st.apply(f"/deckLoaded/{deck}", loaded(path))
    st.apply(f"/updateDeck/{deck}", {"isPlaying": True})


def test_startup_burst_has_no_now_playing(st):
    startup(st)
    assert st.now_playing() is None
    assert all(d["file_path"] is None for d in st.decks.values())
    assert all(not st.is_audible(d) for d in "ABCD")


def test_load_play_on_air_gives_deck_a(st):
    startup(st)
    play(st)
    assert st.now_playing() == "A"
    assert st.decks["A"]["title"] == "Song" and st.decks["A"]["track_length"] == 200.0


def test_loaded_but_not_playing_is_not_audible(st):
    startup(st)
    st.apply("/deckLoaded/A", loaded())
    assert st.now_playing() is None


def test_fader_down_is_not_audible(st):
    startup(st)
    play(st)
    st.apply("/updateChannel/1", {"onAirLevel": 0.0})
    assert st.now_playing() is None
    st.apply("/updateChannel/1", {"onAirLevel": 0.9})
    assert st.now_playing() == "A"
    st.apply("/updateChannel/1", {"isOnAir": False})
    assert st.now_playing() is None


def test_level_must_exceed_threshold(st):
    startup(st)
    play(st)
    st.apply("/updateChannel/1", {"onAirLevel": 0.05})
    assert st.now_playing() is None
    st.apply("/updateChannel/1", {"onAirLevel": 0.06})
    assert st.now_playing() == "A"


def test_unknown_level_is_not_audible(st):
    st.apply("/updateChannel/1", {"isOnAir": True})
    play(st)
    assert st.now_playing() is None  # no onAirLevel seen yet


def test_duplicates_emit_no_changes(st):
    startup(st)
    play(st)
    st.apply("/updateDeck/A", {"isSynced": True, "elapsedTime": 5.0})
    assert st.apply("/updateDeck/A", {"isSynced": True, "elapsedTime": 5.0}) == []
    assert st.apply("/updateChannel/1", {"isOnAir": True}) == []
    assert st.apply("/updateMasterClock", {"deck": None, "bpm": 120}) == []
    assert st.apply("/deckLoaded/A", loaded()) == [] or st.decks["A"]["is_playing"] is False
    # a repeated identical load of the same track must not reset the deck
    st.apply("/deckLoaded/B", {"filePath": ""})
    assert st.apply("/deckLoaded/B", {"filePath": ""}) == []


def test_empty_deck_loaded_clears_the_deck(st):
    startup(st)
    play(st)
    changes = st.apply("/deckLoaded/A", {"filePath": ""})
    d = st.decks["A"]
    assert d["file_path"] is None and d["title"] is None and d["is_playing"] is False
    assert any(c["field"] == "now_playing" and c["new"] is None for c in changes)


def test_master_clock_does_not_drive_now_playing(st):
    startup(st)
    play(st)
    st.apply("/updateMasterClock", {"deck": "A", "bpm": 120})
    st.apply("/updateMasterClock", {"deck": None, "bpm": 120})
    assert st.now_playing() == "A"
    st.apply("/updateMasterClock", {"deck": "B", "bpm": 99})
    assert st.now_playing() == "A" and st.master == {"deck": "B", "bpm": 99.0}


def test_crossfade_picks_the_louder_deck(st, clock):
    startup(st)
    play(st, "A", TRACK_A)
    clock.advance(10)
    play(st, "B", TRACK_B)
    st.apply("/updateChannel/1", {"onAirLevel": 0.7})
    st.apply("/updateChannel/2", {"onAirLevel": 0.3})
    assert st.now_playing() == "A"
    st.apply("/updateChannel/2", {"onAirLevel": 0.8})
    assert st.now_playing() == "B"
    st.apply("/updateChannel/1", {"onAirLevel": 0.0})
    assert st.now_playing() == "B"


def test_tie_goes_to_most_recently_started(st, clock):
    startup(st)
    play(st, "A", TRACK_A)
    clock.advance(5)
    play(st, "B", TRACK_B)
    assert st.now_playing() == "B"  # both at level 1


def test_audible_seconds_only_counts_while_audible(st, clock):
    startup(st)
    play(st)
    for _ in range(5):  # five 1 s ticks like the mod's elapsedTime stream
        clock.advance(1)
        st.apply("/updateDeck/A", {"elapsedTime": clock.t})
    assert st.decks["A"]["audible_seconds"] == pytest.approx(5.0)
    st.apply("/updateChannel/1", {"onAirLevel": 0.0})
    for _ in range(3):
        clock.advance(1)
        st.apply("/updateDeck/A", {"elapsedTime": clock.t})
    assert st.decks["A"]["audible_seconds"] == pytest.approx(5.0)
    st.apply("/updateChannel/1", {"onAirLevel": 1.0})
    clock.advance(2)
    st.apply("/updateDeck/A", {"elapsedTime": clock.t})
    assert st.decks["A"]["audible_seconds"] == pytest.approx(7.0)
    st.apply("/deckLoaded/A", loaded(TRACK_B))  # a different track starts from zero
    assert st.decks["A"]["audible_seconds"] == 0.0


def test_stale_gap_is_not_credited(st, clock):
    startup(st)
    play(st)
    clock.advance(1)
    st.apply("/updateDeck/A", {"elapsedTime": 1.0})
    clock.advance(60)  # Traktor went quiet / disconnected
    st.apply("/updateDeck/A", {"elapsedTime": 2.0})
    assert st.decks["A"]["audible_seconds"] == pytest.approx(1.0)


def test_unknown_input_never_raises(st):
    assert st.apply("/updateDeck/A", {"somethingNew": 5, "isSynced": True}) == []  # raw only: not a change
    assert st.decks["A"]["raw"] == {"somethingNew": 5, "isSynced": True}
    assert st.apply("/nonsense/Z", {"a": 1}) == []
    assert st.apply("/updateDeck/Q", {"a": 1}) == []
    assert st.apply("/updateChannel/9", {"a": 1}) == []
    assert st.apply("/updateDeck/A", "not a dict") == []
    assert st.apply("", {}) == []
    assert st.apply("/updateDeck/A", {"bpm": "fast", "tempo": 0, "elapsedTime": None}) == []
    assert st.ignored_count >= 5


def test_snapshot_hides_file_path_by_default(st, clock):
    startup(st)
    play(st)
    snap = st.snapshot()
    assert "file_path" not in snap["decks"]["A"]
    assert snap["decks"]["A"]["file_name"] == "Artist A - Song A.mp3"
    assert snap["now_playing"] == "A" and snap["decks"]["A"]["audible"] is True
    assert snap["server_time"] == clock.t
    assert "C:" not in json.dumps(snap)
    assert st.snapshot(include_path=True)["decks"]["A"]["file_path"] == TRACK_A
    json.dumps(snap)


def test_last_played_survives_silence(st):
    startup(st)
    play(st)
    st.apply("/updateDeck/A", {"isPlaying": False})
    assert st.now_playing() is None
    assert st.last_played["title"] == "Song" and st.last_played["deck"] == "A"


def test_replaying_committed_samples(st):
    files = sorted(SAMPLES.glob("*.json"))
    assert files, "fixtures/traktor/samples is empty"
    events = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    for e in events:
        st.apply(e["path"], e["body"])
    assert st.now_playing() is None  # these samples have no loaded track
    for e in events:  # the mod repeats itself: the same event twice in a row changes nothing
        st.apply(e["path"], e["body"])
        assert st.apply(e["path"], e["body"]) == []
