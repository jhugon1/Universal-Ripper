import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
PLACES = ("decks", "ripper", "queue", "queue-list", "results", "toast", "played-list", "pl-status", "view-playlist")


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "t.db"))
    with TestClient(app_module.app) as c:
        yield c


def test_the_one_page_is_served_with_every_fixed_place(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    for place in PLACES:
        assert f'id="{place}"' in page.text, place
    assert page.text.count('class="deck"') == 2  # two decks, A and B
    assert "/static/app.js" in page.text and "/static/app.css" in page.text
    assert "ripped_wordmark.png" in page.text and 'rel="icon"' in page.text


def test_the_old_pages_are_gone(client):
    for path in ("/dj", "/booth", "/guest", "/api/request", "/api/download_direct"):
        assert client.get(path).status_code in (404, 405), path


def test_the_splash_shows_on_a_normal_start_and_not_in_dev(client, monkeypatch):
    assert 'id="splash"' in client.get("/").text
    monkeypatch.setenv("RIPPER_DEV", "1")
    assert 'id="splash"' not in client.get("/").text


def test_every_static_file_the_page_uses_is_served(client):
    for path in ("/static/app.css", "/static/app.js", "/static/ripped_wordmark.png", "/static/ripped_face.jpg",
                 "/static/fonts/Inter.woff2", "/static/fonts/SpaceGrotesk.woff2"):
        got = client.get(path)
        assert got.status_code == 200 and len(got.content) > 1000, path
    css = client.get("/static/app.css").text
    assert "/static/fonts/Inter.woff2" in css and "#F5A524" in css and "#101010" in css  # the approved tokens


def test_the_wordmark_is_the_agreed_crop():
    data = (ROOT / "static" / "ripped_wordmark.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", data[16:24]) == (935, 268)
    assert (ROOT / "static" / "ripped_face.jpg").stat().st_size > 10_000


def test_the_page_script_is_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    done = subprocess.run([node, "--check", str(ROOT / "static" / "app.js")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def make_stripe(folder, key, columns=65536, loaded=None):
    import os
    sub = folder / "001"
    sub.mkdir(parents=True, exist_ok=True)
    body = bytearray(b"\x06PRTS" + b"\x00" * 20)
    for i in range(columns):
        v = i % 250
        body += bytes([v // 4, v // 3, v // 2, v])
    (sub / key).write_bytes(bytes(body))
    (sub / (key + ".stripe")).write_text("22 serialization::archive")
    if loaded:
        os.utime(sub / (key + ".stripe"), (loaded, loaded))


def test_stripes_are_read_and_matched_to_the_track_loaded_at_that_time(tmp_path):
    from traktor import stripes
    make_stripe(tmp_path, "OLDKEY", loaded=1000)
    make_stripe(tmp_path, "NEWKEY", loaded=2000)
    found = stripes.list_stripes(str(tmp_path))
    assert set(found) == {"OLDKEY", "NEWKEY"}
    assert stripes.pick_stripe(found, loaded_at=1999) == "NEWKEY"          # touched right after the load
    assert stripes.pick_stripe(found, loaded_at=2006) == "NEWKEY"          # or a few seconds BEFORE the mod reported it
    assert stripes.pick_stripe(found, loaded_at=1500) is None              # nothing touched near that moment
    assert stripes.pick_stripe(found, loaded_at=1999, taken={"NEWKEY"}) is None   # not one another track already has
    cols = stripes.read_columns(found["NEWKEY"][1], 16384)
    assert len(cols) == 16384 * 4 and max(cols[3::4]) > 200
    (tmp_path / "001" / "BAD").write_bytes(b"nope")
    assert stripes.read_columns(str(tmp_path / "001" / "BAD")) is None


def test_waveform_endpoint_finds_the_stripe_of_the_loaded_deck_and_remembers_it(tmp_path, monkeypatch):
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RIPPER_STRIPES_DIR", str(tmp_path))
    make_stripe(tmp_path, "KEYONE", loaded=5000)
    with TestClient(app_module.app) as c:
        assert c.get("/api/waveform/A").status_code == 404                     # nothing loaded
        app_module.traktor_state.decks["A"]["file_path"] = "C:\\x\\a.mp3"
        app_module.traktor_state.decks["A"]["loaded_at"] = 4999
        got = c.get("/api/waveform/a")
        assert got.status_code == 200 and len(got.content) == 65536 * 4 and got.headers["x-columns"] == "65536"
        app_module.traktor_state.decks["A"]["loaded_at"] = 9999999999          # later loads reuse the remembered key
        assert c.get("/api/waveform/A").status_code == 200
        assert c.get("/api/waveform/Z").status_code == 404


def test_a_hard_link_of_a_matched_track_reuses_its_waveform(tmp_path, monkeypatch):
    import os
    import app as app_module
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RIPPER_STRIPES_DIR", str(tmp_path / "stripes"))
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "own.db"))   # its own database: no key claimed by other tests
    (tmp_path / "stripes").mkdir()
    make_stripe(tmp_path / "stripes", "KEYONE", loaded=5000)
    song, link = tmp_path / "song.mp3", tmp_path / "tonight.mp3"
    song.write_bytes(b"x")
    os.link(song, link)
    with TestClient(app_module.app) as c:
        decks = app_module.traktor_state.decks
        decks["A"]["file_path"], decks["A"]["loaded_at"] = str(song), 4999
        assert c.get("/api/waveform/A").status_code == 200
        decks["B"]["file_path"], decks["B"]["loaded_at"] = str(link), 9999999999   # nothing touched near this load
        assert c.get("/api/waveform/B").status_code == 200
