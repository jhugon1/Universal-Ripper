import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
PLACES = ("decks", "ripper", "queue", "queue-list", "results", "hint", "toast", "played-list", "pl-status", "view-playlist")


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
    assert "/static/fonts/Inter.woff2" in css and "#F5A524" in css and "#0E0F12" in css  # the approved tokens


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
