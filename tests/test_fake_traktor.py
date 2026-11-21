import asyncio
import importlib.util
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from traktor.receiver import TraktorReceiver
from traktor.state import TraktorState

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fake_traktor", ROOT / "tools" / "fake_traktor.py")
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)

REPLAY = ROOT / "fixtures" / "traktor" / "replays" / "capture_startup_load_play.jsonl"
SAMPLES = ROOT / "fixtures" / "traktor" / "samples"


def play_through(events):
    """Feed events into a fresh state on a simulated clock; return the now_playing seen after each."""
    clock = [1000.0]
    st = TraktorState(clock=lambda: clock[0])
    seen = []
    for delay, path, body in events:
        clock[0] += delay
        st.apply(path, body)
        seen.append(st.now_playing())
    return st, seen


def sequence(seen):
    """Distinct consecutive non-None values: ["A", "B"] means A was on, then B."""
    out = []
    for v in seen:
        if v is not None and (not out or out[-1] != v):
            out.append(v)
    return out


@pytest.mark.parametrize("name", sorted(ft.SCENARIOS))
def test_every_scenario_is_valid(name):
    events = list(ft.SCENARIOS[name]().events())
    assert events
    for delay, path, body in events:
        assert delay >= 0 and path.startswith("/") and isinstance(body, dict)
        json.dumps(body)
    play_through(events)  # must not raise


def test_startup_scenario_shows_nothing_playing():
    st, seen = play_through(ft.SCENARIOS["startup"]().events())
    assert sequence(seen) == [] and st.now_playing() is None


def test_play_a_scenario():
    st, seen = play_through(ft.SCENARIOS["play_a"]().events())
    assert sequence(seen) == ["A"]
    assert seen[-1] is None  # paused at the end
    assert st.decks["A"]["audible_seconds"] == pytest.approx(30.0, abs=1.5)


def test_crossfade_scenario_goes_a_then_b():
    st, seen = play_through(ft.SCENARIOS["crossfade_a_b"]().events())
    assert sequence(seen) == ["A", "B"]
    assert seen[-1] == "B"
    assert None not in seen[seen.index("A"):]  # never silent while crossing over


def test_all_decks_scenario():
    st, seen = play_through(ft.SCENARIOS["all_decks"]().events())
    assert sequence(seen) == ["A", "B"]
    assert all(st.decks[d]["file_path"] for d in "ABCD")
    assert not st.decks["C"]["is_playing"] and not st.decks["D"]["is_playing"]


def test_long_titles_scenario():
    st, seen = play_through(ft.SCENARIOS["long_titles"]().events())
    assert sequence(seen) == ["A", "B"]
    assert st.decks["A"]["title"].startswith("日本語")  # reloaded with a unicode title at the end
    assert st.decks["B"]["title"] is None and st.snapshot()["decks"]["B"]["file_name"] == "Test Artist B - Unlabeled Track.mp3"
    longest = max(len(e[2].get("title") or "") for e in ft.SCENARIOS["long_titles"]().events())
    assert longest > 250


def test_disconnect_scenario_just_stops():
    st, seen = play_through(ft.SCENARIOS["disconnect"]().events())
    assert seen[-1] == "A"  # no pause event: the state is left looking live
    assert st.decks["A"]["is_playing"] is True


def test_replay_capture_fixture():
    events = list(ft.load_replay(REPLAY))
    lines = [json.loads(l) for l in REPLAY.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(events) == len(lines) > 50
    assert events[0][0] == 0.0 and all(d >= 0 for d, _, _ in events)
    assert sum(d for d, _, _ in events) == pytest.approx(lines[-1]["t"] - lines[0]["t"], abs=0.01)
    st, seen = play_through(events)
    assert "A" in seen, "the real capture plays deck A through an open channel"
    assert st.decks["A"]["title"] == "the live set"
    assert st.snapshot()["decks"]["A"]["file_name"] == "the live set.wav"
    assert seen[:5] == [None] * 5  # the startup burst never counts as playing


def test_replay_folder_of_samples_uses_fixed_gaps():
    events = list(ft.load_replay(SAMPLES, gap=0.5))
    assert events[0][0] == 0.0 and {e[0] for e in events[1:]} == {0.5}
    assert len(events) == len(list(SAMPLES.glob("*.json")))


def test_replay_fixture_has_no_personal_data():
    text = REPLAY.read_text(encoding="utf-8")
    assert "Users" not in text and Path.home().name.lower() not in text.lower()


def test_run_events_divides_delays_by_speed_and_survives_failures():
    slept, posted = [], []

    def post(path, body):
        posted.append(path)
        return len(posted) != 2  # the second post "fails"

    n = ft.run_events([(0.0, "/a", {}), (2.0, "/b", {}), (4.0, "/c", {})], post, speed=4.0, sleep=slept.append)
    assert n == 3 and posted == ["/a", "/b", "/c"] and slept == [0.5, 1.0]


def test_cli_list_and_bad_arguments(capsys):
    assert ft.main(["--list"]) == 0
    assert "crossfade_a_b" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ft.main([])
    with pytest.raises(SystemExit):
        ft.main(["--scenario", "nope"])


# ---- end to end against the real receiver ----------------------------------------------------
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningReceiver:
    def __init__(self, state, port):
        self.state, self.port = state, port
        self._stop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        async def main():
            self._stop = asyncio.Event()
            r = TraktorReceiver(self.state, self.port)
            r.start()
            await self._stop.wait()
            await r.stop()

        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(main())

    def __enter__(self):
        self._thread.start()
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("receiver did not start")

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=5)


def test_cli_end_to_end_against_the_receiver(capsys):
    state = TraktorState()
    port = free_port()
    with RunningReceiver(state, port):
        rc = ft.main(["--scenario", "play_a", "--speed", "500", "--url", f"http://127.0.0.1:{port}"])
    assert rc == 0
    assert "sent" in capsys.readouterr().out
    assert state.decks["A"]["title"] == "Test Track A"
    assert state.event_count > 30
