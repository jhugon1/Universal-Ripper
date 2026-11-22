import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ripper_queue import playlists, spotify, store

ROOT = Path(__file__).resolve().parent.parent
PID = "2objzKylZ5fIYrdKYMkHHt"
CID = "a" * 32


# ---- the login pieces ----------------------------------------------------------------------------
def test_pkce_challenge_matches_the_rfc_example():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # RFC 7636, appendix B
    assert spotify.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    v = spotify.new_verifier()
    assert 43 <= len(v) <= 128 and v != spotify.new_verifier()


def test_authorize_url_asks_for_the_playlist_scopes_with_pkce():
    url = spotify.authorize_url(CID, "http://127.0.0.1:8000/api/spotify/callback", "st4te", "chal")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert q["client_id"] == [CID] and q["response_type"] == ["code"] and q["state"] == ["st4te"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"] == ["chal"]
    assert set(q["scope"][0].split()) == {"playlist-read-private", "playlist-read-collaborative"}
    assert q["redirect_uri"] == ["http://127.0.0.1:8000/api/spotify/callback"]  # the loopback address, never "localhost"


def test_redirect_uri_follows_the_port(monkeypatch):
    assert spotify.redirect_uri(9000) == "http://127.0.0.1:9000/api/spotify/callback"
    monkeypatch.setenv("RIPPER_PORT", "8123")
    assert spotify.redirect_uri() == "http://127.0.0.1:8123/api/spotify/callback"


@pytest.mark.parametrize("text", [
    f"https://open.spotify.com/playlist/{PID}", f"https://open.spotify.com/playlist/{PID}?si=abc123",
    f"https://open.spotify.com/intl-de/playlist/{PID}", f"https://open.spotify.com/user/someone/playlist/{PID}",
    f"spotify:playlist:{PID}", f"  https://open.spotify.com/playlist/{PID}  ",
])
def test_playlist_links(text):
    assert spotify.playlist_id(text) == PID


@pytest.mark.parametrize("text", ["", None, "https://open.spotify.com/track/" + PID, "https://open.spotify.com/playlist/short",
                                  "https://example.com/playlist/" + PID, "spotify:track:" + PID])
def test_not_a_playlist_link(text):
    assert spotify.playlist_id(text) is None


# ---- tokens ----------------------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    conn.execute(spotify.SCHEMA)
    yield conn
    conn.close()


class FakeHttp:
    """Answers by URL; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers, data))
        for needle, answer in self.routes.items():
            if needle in url:
                status, body = answer(url) if callable(answer) else answer
                return status, body, {"Retry-After": "30"}
        raise AssertionError("unexpected request " + url)


def test_exchange_stores_tokens_and_a_fresh_token_is_reused(db):
    http = FakeHttp({"/api/token": (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})})
    spotify.exchange_code(db, CID, "the-code", "the-verifier", "http://127.0.0.1:8000/cb", http=http, now=1000.0)
    sent = http.calls[0][3]
    assert sent["grant_type"] == "authorization_code" and sent["code_verifier"] == "the-verifier" and "client_secret" not in sent
    assert spotify.access_token(db, CID, http=http, now=1500.0) == "AT1" and len(http.calls) == 1  # no refresh needed


def test_an_expiring_token_is_refreshed_and_the_refresh_token_kept(db):
    http = FakeHttp({"/api/token": (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600})})
    spotify.exchange_code(db, CID, "c", "v", "r", http=http, now=1000.0)
    http.routes["/api/token"] = (200, {"access_token": "AT2", "expires_in": 3600})  # Spotify may omit a new refresh token
    assert spotify.access_token(db, CID, http=http, now=4600.0) == "AT2"
    assert http.calls[-1][3]["grant_type"] == "refresh_token" and http.calls[-1][3]["refresh_token"] == "RT1"
    assert spotify.load_tokens(db)["refresh_token"] == "RT1"


def test_what_is_missing_is_named(db):
    with pytest.raises(spotify.SpotifyError) as setup:
        spotify.access_token(db, None, http=FakeHttp({}))
    assert setup.value.action == "setup"
    with pytest.raises(spotify.SpotifyError) as login:
        spotify.access_token(db, CID, http=FakeHttp({}))
    assert login.value.action == "login"
    http = FakeHttp({"/api/token": (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 10})})
    spotify.exchange_code(db, CID, "c", "v", "r", http=http, now=0.0)
    with pytest.raises(spotify.SpotifyError) as other:  # a different Client ID than the one that logged in
        spotify.access_token(db, "b" * 32, http=http, now=1.0)
    assert other.value.action == "login"


def test_a_refused_refresh_asks_for_a_new_login_and_forgets_the_tokens(db):
    http = FakeHttp({"/api/token": (200, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 10})})
    spotify.exchange_code(db, CID, "c", "v", "r", http=http, now=0.0)
    http.routes["/api/token"] = (400, {"error": "invalid_grant"})
    with pytest.raises(spotify.SpotifyError) as err:
        spotify.access_token(db, CID, http=http, now=100.0)
    assert err.value.action == "login" and spotify.load_tokens(db) is None


def test_a_login_spotify_rejects_is_reported(db):
    http = FakeHttp({"/api/token": (400, {"error": "invalid_grant", "error_description": "Invalid authorization code"})})
    with pytest.raises(spotify.SpotifyError, match="Invalid authorization code"):
        spotify.exchange_code(db, CID, "bad", "v", "r", http=http)


# ---- reading a playlist ---------------------------------------------------------------------------
def track(n, **kw):
    t = {"name": f"Song {n}", "duration_ms": 180000 + n * 1000, "is_local": False, "type": "track",
         "artists": [{"name": "Band"}, {"name": f"Guest {n}"}], "album": {"name": f"Album {n}"}}
    t.update(kw)
    return {"track": t}


def playlist_http(pages, name="My Mix", total=None):
    def tracks(url):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
        index = offset // 2
        page = pages[index]
        more = index + 1 < len(pages)
        return 200, {"items": page, "next": "https://api.spotify.com/next" if more else None, "total": total}

    return FakeHttp({f"/playlists/{PID}/tracks": tracks,
                     f"/playlists/{PID}": (200, {"name": name, "tracks": {"total": total}})})


def test_a_playlist_is_read_in_order_across_pages():
    http = playlist_http([[track(1), track(2)], [track(3), track(4)], [track(5)]], total=5)
    got = spotify.fetch_playlist(f"https://open.spotify.com/playlist/{PID}?si=x", "TOKEN", http=http)
    assert got["name"] == "My Mix" and [t["title"] for t in got["tracks"]] == [f"Song {n}" for n in range(1, 6)]
    assert got["tracks"][0] == {"position": 1, "artist": "Band, Guest 1", "title": "Song 1", "album": "Album 1", "duration_s": 181.0}
    assert [t["position"] for t in got["tracks"]] == [1, 2, 3, 4, 5] and got["claimed_count"] == 5 and got["skipped"] == 0
    assert all(c[2]["Authorization"] == "Bearer TOKEN" for c in http.calls)


def test_local_files_episodes_and_removed_tracks_are_skipped_and_positions_stay_contiguous():
    http = playlist_http([[track(1), {"track": None}], [track(2, is_local=True), track(3, type="episode")], [track(4)]], total=5)
    got = spotify.fetch_playlist(f"spotify:playlist:{PID}", "T", http=http)
    assert [(t["position"], t["title"]) for t in got["tracks"]] == [(1, "Song 1"), (2, "Song 4")]
    assert got["skipped"] == 3 and got["claimed_count"] == 2


def test_the_playlist_result_feeds_the_normal_flow():
    http = playlist_http([[track(1), track(2)]], name="Road/Trip: 2026?", total=2)
    parsed = spotify.fetch_playlist(f"spotify:playlist:{PID}", "T", http=http)
    result = playlists.fetch_playlist(f"https://open.spotify.com/playlist/{PID}", spotify=lambda url: parsed)
    assert result["service"] == "spotify" and result["count"] == 2 and result["total_s"] == 363.0
    assert result["folder_name"] == "RoadTrip 2026 (6m)" and result["truncated"] is False


def test_spotify_problems_are_named():
    link = f"https://open.spotify.com/playlist/{PID}"
    for status, text in ((401, "log in again"), (403, "does not let this account"), (404, "could not find"), (429, "limiting"), (500, "error")):
        with pytest.raises(spotify.SpotifyError, match=text):
            spotify.fetch_playlist(link, "T", http=FakeHttp({"/playlists/": (status, {})}))
    with pytest.raises(spotify.SpotifyError) as err:
        spotify.fetch_playlist(link, "T", http=FakeHttp({"/playlists/": (401, {})}))
    assert err.value.action == "login"
    with pytest.raises(spotify.SpotifyError, match="not a Spotify playlist"):
        spotify.fetch_playlist("https://example.com", "T", http=FakeHttp({}))
    with pytest.raises(spotify.SpotifyError, match="No tracks"):
        spotify.fetch_playlist(link, "T", http=playlist_http([[{"track": None}]], total=1))


def test_the_renamed_items_endpoint_is_tried_when_tracks_is_gone():
    calls = []

    def route(url):
        calls.append(url)
        if "/tracks" in url:
            return 404, {}
        return 200, {"items": [track(1)], "next": None, "total": 1}

    http = FakeHttp({f"/playlists/{PID}/": route, f"/playlists/{PID}?": (200, {"name": "N", "tracks": {"total": 1}})})
    got = spotify.fetch_playlist(f"spotify:playlist:{PID}", "T", http=http)
    assert [t["title"] for t in got["tracks"]] == ["Song 1"] and any("/items" in c for c in calls)


# ---- through the app --------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("RIPPER_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("RIPPER_SPOTIFY_CLIENT_ID", raising=False)
    with TestClient(app_module.app, follow_redirects=False) as c:
        yield c, app_module


def test_setup_login_and_fetch_through_the_app(client, monkeypatch):
    c, app_module = client
    assert c.get("/api/spotify/status").json() == {"client_id_set": False, "connected": False,
                                                    "redirect_uri": "http://127.0.0.1:8000/api/spotify/callback"}
    # no Client ID yet: the page is told to offer setup
    blocked = c.post("/api/playlist/fetch", data={"url": f"https://open.spotify.com/playlist/{PID}"})
    assert blocked.status_code == 400 and blocked.json()["action"] == "setup"
    assert c.get("/api/spotify/login").status_code == 400
    assert c.post("/api/spotify/client_id", data={"client_id": "not a client id"}).status_code == 400
    assert c.post("/api/spotify/client_id", data={"client_id": CID}).json()["client_id_set"] is True
    # a Client ID but no login yet: the page is told to offer the login
    needs_login = c.post("/api/playlist/fetch", data={"url": f"https://open.spotify.com/playlist/{PID}"}).json()
    assert needs_login["action"] == "login"

    sent = c.get("/api/spotify/login")  # the browser is sent to Spotify
    assert sent.status_code in (302, 307)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(sent.headers["location"]).query)
    assert q["client_id"] == [CID] and q["code_challenge_method"] == ["S256"]

    http = FakeHttp({"/api/token": (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}),
                     f"/playlists/{PID}/tracks": (200, {"items": [track(1), track(2)], "next": None, "total": 2}),
                     f"/playlists/{PID}?": (200, {"name": "My Mix", "tracks": {"total": 2}})})
    monkeypatch.setattr(spotify, "http_json", http)
    assert c.get("/api/spotify/callback", params={"code": "c", "state": "wrong"}).status_code == 400  # a login we never started
    ok = c.get("/api/spotify/callback", params={"code": "the-code", "state": q["state"][0]})
    assert ok.status_code == 200 and "connected" in ok.text
    assert http.calls[0][3]["client_id"] == CID and http.calls[0][3]["code"] == "the-code"
    assert c.get("/api/spotify/status").json()["connected"] is True

    got = c.post("/api/playlist/fetch", data={"url": f"https://open.spotify.com/playlist/{PID}"}).json()
    assert (got["service"], got["name"], got["count"]) == ("spotify", "My Mix", 2)
    assert c.post("/api/spotify/disconnect").json()["connected"] is False


def test_the_callback_page_does_not_echo_markup(client):
    c, _ = client
    page = c.get("/api/spotify/callback", params={"error": "<script>alert(1)</script>"})
    assert page.status_code == 400 and "<script>" not in page.text and "&lt;script&gt;" in page.text
