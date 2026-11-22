"""Spotify playlists through the user's OWN Spotify developer app.

Why this way: Spotify's shared public keys (the ones spotdl uses) are over quota, and personal (private)
playlists can only be read with the user's permission anyway. So the user makes a free developer app once,
and logs in once in the browser (authorization code flow with PKCE: no client secret is needed). The tokens
are kept in the local database and refreshed automatically.

Setup (done by the user, see the report): create an app at developer.spotify.com/dashboard with the redirect
URI shown by `redirect_uri()` (Spotify requires the 127.0.0.1 form, not "localhost"), then give the app its
Client ID (RIPPER_SPOTIFY_CLIENT_ID, or the box on the page).
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative"
SCHEMA = """CREATE TABLE IF NOT EXISTS spotify_auth
            (id INTEGER PRIMARY KEY CHECK (id = 1), client_id TEXT, access_token TEXT, refresh_token TEXT, expires_at REAL)"""
_ID = r"([A-Za-z0-9]{22})"


class SpotifyError(Exception):
    """A Spotify problem worded for the person using the page. `action` says what the page can offer:
    "setup" (needs a Client ID), "login" (needs the one-time login) or None."""

    def __init__(self, message, action=None):
        super().__init__(message)
        self.action = action


# ---- the pieces of the login ------------------------------------------------------------------
def redirect_uri(port=None) -> str:
    return f"http://127.0.0.1:{port or os.environ.get('RIPPER_PORT') or 8000}/api/spotify/callback"


def new_verifier() -> str:
    return secrets.token_urlsafe(64)  # 86 characters: inside the 43-128 Spotify allows


def code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url(client_id, redirect, state, challenge) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge})


def playlist_id(url):
    """The 22-character id in a Spotify playlist link (open.spotify.com/playlist/..., /user/x/playlist/..., spotify:playlist:...)."""
    text = (url or "").strip()
    for pattern in (rf"open\.spotify\.com/(?:intl-[a-z]+/)?playlist/{_ID}", rf"open\.spotify\.com/user/[^/]+/playlist/{_ID}",
                    rf"^spotify:playlist:{_ID}", rf"^spotify:user:[^:]+:playlist:{_ID}"):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


# ---- talking to Spotify -----------------------------------------------------------------------
def http_json(method, url, headers=None, data=None, timeout=30):
    """(status, parsed JSON or {}, response headers). A Spotify error status is returned, not raised."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw, status, hdrs = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read(), exc.code, exc.headers
    except OSError as exc:
        raise SpotifyError(f"Could not reach Spotify ({type(exc).__name__}).") from exc
    try:
        return status, json.loads(raw or b"{}"), hdrs
    except ValueError:
        return status, {}, hdrs


def load_tokens(conn):
    row = conn.execute("SELECT client_id, access_token, refresh_token, expires_at FROM spotify_auth WHERE id = 1").fetchone()
    return dict(row) if row else None


def save_tokens(conn, client_id, tokens, now=None):
    now = time.time() if now is None else now
    old = load_tokens(conn) or {}
    conn.execute("INSERT OR REPLACE INTO spotify_auth (id, client_id, access_token, refresh_token, expires_at) VALUES (1, ?, ?, ?, ?)",
                 (client_id, tokens["access_token"], tokens.get("refresh_token") or old.get("refresh_token"),
                  now + float(tokens.get("expires_in", 3600))))
    conn.commit()


def forget_tokens(conn):
    conn.execute("DELETE FROM spotify_auth")
    conn.commit()


def exchange_code(conn, client_id, code, verifier, redirect, http=http_json, now=None):
    status, body, _ = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                           {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                            "client_id": client_id, "code_verifier": verifier})
    if status != 200 or "access_token" not in body:
        raise SpotifyError(f"Spotify did not accept the login ({body.get('error_description') or body.get('error') or status}).")
    save_tokens(conn, client_id, body, now)


def access_token(conn, client_id, http=http_json, now=None) -> str:
    """A valid access token, refreshed when it is about to expire. Raises SpotifyError saying what is missing."""
    now = time.time() if now is None else now
    if not client_id:
        raise SpotifyError("Spotify needs your Client ID first.", action="setup")
    saved = load_tokens(conn)
    if not saved or not saved.get("refresh_token") or saved.get("client_id") != client_id:
        raise SpotifyError("Connect Spotify first (a one-time login).", action="login")
    if saved["access_token"] and saved["expires_at"] - 60 > now:
        return saved["access_token"]
    status, body, _ = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"},
                           {"grant_type": "refresh_token", "refresh_token": saved["refresh_token"], "client_id": client_id})
    if status != 200 or "access_token" not in body:
        forget_tokens(conn)
        raise SpotifyError("Spotify asks you to log in again.", action="login")
    save_tokens(conn, client_id, body, now)
    return body["access_token"]


def _get(path, token, http, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    status, body, headers = http("GET", url, {"Authorization": "Bearer " + token})
    if status == 200:
        return body
    if status == 401:
        raise SpotifyError("Spotify asks you to log in again.", action="login")
    if status == 403:
        raise SpotifyError("Spotify does not let this account read that playlist (private, or restricted by Spotify).")
    if status == 404:
        raise SpotifyError("Spotify could not find that playlist.")
    if status == 429:
        wait = headers.get("Retry-After") if headers else None
        raise SpotifyError(f"Spotify is limiting requests right now{f' (try again in {wait} s)' if wait else ''}.")
    raise SpotifyError(f"Spotify answered with an error ({status}).")


def fetch_playlist(url, token, http=http_json) -> dict:
    """Name and tracks (in order, with durations) of a Spotify playlist, in the shape playlists.py expects.
    Local files and podcast episodes cannot be ripped and are skipped (counted in `skipped`)."""
    pid = playlist_id(url)
    if not pid:
        raise SpotifyError("That is not a Spotify playlist link.")
    head = _get(f"/playlists/{pid}", token, http, {"fields": "name,tracks.total"})
    wanted = {"fields": "items(track(name,duration_ms,is_local,type,artists(name),album(name))),next,total", "limit": 100}
    tracks, skipped, offset, total = [], 0, 0, (head.get("tracks") or {}).get("total")
    while True:
        try:
            page = _get(f"/playlists/{pid}/tracks", token, http, {**wanted, "offset": offset})
        except SpotifyError as exc:
            if offset == 0 and "could not find" in str(exc):  # Spotify has been renaming this endpoint: try the new name
                page = _get(f"/playlists/{pid}/items", token, http, {**wanted, "offset": offset})
            else:
                raise
        for item in page.get("items") or []:
            track = (item or {}).get("track") or {}
            if not track.get("name") or track.get("is_local") or track.get("type") not in (None, "track"):
                skipped += 1
                continue
            artists = [a.get("name") for a in track.get("artists") or [] if a.get("name")]
            tracks.append({"position": len(tracks) + 1, "artist": ", ".join(artists) or "Unknown Artist", "title": track["name"],
                           "album": (track.get("album") or {}).get("name"),
                           "duration_s": (track.get("duration_ms") or 0) / 1000.0 or None})
        offset += len(page.get("items") or [])
        if not page.get("next") or not page.get("items"):
            break
        total = page.get("total", total)
    if not tracks:
        raise SpotifyError("No tracks that can be ripped were found in that playlist.")
    return {"name": head.get("name") or "Spotify Playlist", "tracks": tracks,
            "claimed_count": total - skipped if isinstance(total, int) else None, "skipped": skipped}
