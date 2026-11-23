"""The waveform computed from the audio file: bands, cache, and the deck endpoint."""
import math
import shutil
import struct
import time
import wave

import pytest
from fastapi.testclient import TestClient

from ripper_queue import waveform

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def tone_file(path, hz, seconds=1.0, amp=0.8, rate=22050):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * hz * i / rate))) for i in range(int(rate * seconds))))
    return path


def band_peaks(cols):
    return [max(cols[j::3]) for j in range(3)]


def test_pcm_peaks_per_band_and_column():
    # two columns of 3 interleaved channels: low loud, mid quiet, high silent
    hop = 4
    frames = [(10000, 100, 0)] * hop + [(0, 0, -20000)] * hop
    raw = b"".join(struct.pack("<hhh", *f) for f in frames)
    cols = waveform.columns_from_pcm(raw, hop=hop, gain=(1.0, 1.0, 1.0))
    assert list(cols) == [int(10000 * 255 / 32768), int(100 * 255 / 32768), 0, 0, 0, int(20000 * 255 / 32768)]


@needs_ffmpeg
def test_a_low_tone_lands_in_the_low_band_and_a_high_tone_in_the_high_band(tmp_path):
    low = waveform.compute(tone_file(tmp_path / "low.wav", 100))
    high = waveform.compute(tone_file(tmp_path / "high.wav", 9000))
    mid = waveform.compute(tone_file(tmp_path / "mid.wav", 2000))
    lp, mp, hp = band_peaks(low), band_peaks(mid), band_peaks(high)
    assert lp[0] > 60 and lp[1] < 20 and lp[2] < 10          # 100 Hz: low
    assert mp[1] > 60 and mp[1] > 2.5 * max(mp[0], mp[2])    # 2 kHz: mid (simple filters leak a little)
    assert hp[2] > 60 and hp[1] < 20 and hp[0] < 10          # 9 kHz: high
    assert 480 <= len(low) // 3 <= 510                        # 1 s of audio = ~500 columns of 2 ms


def test_cache_computes_once_in_the_background_and_remembers(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPER_WAVEFORM_DIR", str(tmp_path / "cache"))
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")
    calls = []

    def fake(path):
        calls.append(path)
        time.sleep(0.1)
        return bytes([1, 2, 3] * 10)

    cache = waveform.WaveformCache(fake)
    assert cache.get(str(song)) is None                       # answers at once, computing in the background
    for _ in range(50):
        got = cache.get(str(song))
        if got:
            break
        time.sleep(0.05)
    assert got == (10, bytes([1, 2, 3] * 10))
    assert waveform.WaveformCache(fake).get(str(song)) == got  # a new run reads the file, no recompute
    assert len(calls) == 1
    song.write_bytes(b"changed file")                          # a changed file is a new waveform
    assert cache.get(str(song)) is None


def test_unreadable_audio_is_remembered_as_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPER_WAVEFORM_DIR", str(tmp_path / "cache"))
    song = tmp_path / "bad.mp3"
    song.write_bytes(b"not audio")

    def boom(path):
        raise RuntimeError("ffmpeg could not read the audio")

    cache = waveform.WaveformCache(boom)
    cache.get(str(song))
    for _ in range(50):
        if cache.failed(str(song)):
            break
        time.sleep(0.05)
    assert cache.failed(str(song)) and cache.get(str(song)) is None


def test_pack_unpack_round_trip_and_rejects_garbage():
    cols = bytes(range(30))
    assert waveform.unpack(waveform.pack(cols)) == (10, cols)
    assert waveform.unpack(b"nope") is None and waveform.unpack(waveform.pack(cols)[:-1]) is None


def test_deck_waveform_endpoint(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "own.db"))
    song = tmp_path / "a.mp3"
    song.write_bytes(b"x")
    answers = {}
    monkeypatch.setattr(waveform.CACHE, "get", lambda p: answers.get("get"))
    monkeypatch.setattr(waveform.CACHE, "failed", lambda p: answers.get("failed", False))
    with TestClient(app_module.app) as c:
        assert c.get("/api/waveform/A").status_code == 404            # nothing loaded
        app_module.traktor_state.decks["A"]["file_path"] = str(song)
        assert c.get("/api/waveform/A").status_code == 202            # still computing
        answers["get"] = (2, bytes([9, 8, 7, 6, 5, 4]))
        got = c.get("/api/waveform/a")
        assert got.status_code == 200 and waveform.unpack(got.content) == (2, bytes([9, 8, 7, 6, 5, 4]))
        answers.update(get=None, failed=True)
        assert c.get("/api/waveform/A").status_code == 404            # unreadable: stop asking
        assert c.get("/api/waveform/Z").status_code == 404
