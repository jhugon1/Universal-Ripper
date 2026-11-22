"""Playlist rips: fetch a playlist, then rip it into `Playlist Rips\\<Name> (<length>)\\NN Artist - Title.mp3`.

Every track goes through the library like any other rip (same dedupe by YouTube video id: a track already in
the library is linked, never downloaded again); the playlist folder holds hard links (or copies) named by
playlist position. Playlist rips are NOT added to tonight's queue.

Re-ripping a playlist whose folder already exists updates it in place: missing tracks are linked, nothing
that is already there is touched. A different total length gives a different folder name, so a changed
playlist gets a new folder.
"""
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from filenames import clean_name, track_basename

from . import files, rip, search, store

APPLE_DATA = re.compile(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', re.S)
SPOTIFY_UNAVAILABLE = "Spotify is not set up in this app. Apple Music links work."


class PlaylistError(Exception):
    """A problem with a playlist link, worded for the person using the page."""


def playlist_dir() -> Path:
    env = os.environ.get("RIPPER_PLAYLIST_DIR")
    return Path(env) if env else Path.home() / "Music" / "Traktor" / "Playlist Rips"


# ---- naming ----------------------------------------------------------------------------------
def length_label(total_s) -> str:
    """'1h31m' for 5460 s, '45m' under an hour, '?' when the length is not known (rounded to the nearest minute)."""
    if not total_s or total_s <= 0:
        return "?"
    minutes = int(total_s // 60) + (1 if total_s % 60 >= 30 else 0)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def folder_name(name, total_s) -> str:
    """`<Playlist Name> (1h31m)`, safe as a folder name on any system."""
    return f"{clean_name(name) or 'Playlist'} ({length_label(total_s)})"


def track_filename(position, count, artist, title) -> str:
    """`NN Artist - Title.mp3`; the number is as wide as the biggest position (two digits at least)."""
    width = max(2, len(str(count)))
    return f"{position:0{width}d} {track_basename(artist, title)}.mp3"


def service_of(url):
    url = (url or "").lower()
    if "music.apple.com" in url:
        return "apple"
    if "open.spotify.com" in url or "spotify.link" in url or url.startswith("spotify:"):
        return "spotify"
    return None


# ---- fetching --------------------------------------------------------------------------------
def parse_apple_page(html: str) -> dict:
    """Name and tracks (in playlist order, with durations) from an Apple Music playlist page."""
    match = APPLE_DATA.search(html or "")
    if not match:
        raise PlaylistError("Could not read this Apple Music page (its format may have changed).")
    try:
        sections = json.loads(match.group(1))["data"][0]["data"]["sections"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise PlaylistError(f"Could not read the Apple Music playlist data ({type(exc).__name__}).") from exc
    header = next((s for s in sections if s.get("itemKind") == "containerDetailHeaderLockup"), None)
    head = (header or {}).get("items") or [{}]
    name = head[0].get("title") or "Apple Music Playlist"
    try:
        claimed = int(head[0].get("trackCount"))
    except (TypeError, ValueError):
        claimed = None
    section = next((s for s in sections if s.get("itemKind") == "trackLockup"), None)
    if not section or not section.get("items"):
        raise PlaylistError("No tracks found on this Apple Music playlist page.")
    tracks = []
    for item in section["items"]:
        title = item.get("title")
        if not title:
            continue
        links = item.get("subtitleLinks") or []
        artist = item.get("artistName") or (links[0].get("title") if links else None) or "Unknown Artist"
        albums = item.get("tertiaryLinks") or []
        try:
            seconds = float(item.get("duration")) / 1000.0
        except (TypeError, ValueError):
            seconds = None
        tracks.append({"position": len(tracks) + 1, "artist": artist, "title": title,
                       "album": albums[0].get("title") if albums else None, "duration_s": seconds})
    return {"name": name, "tracks": tracks, "claimed_count": claimed}


def _finish(service, parsed) -> dict:
    tracks = parsed["tracks"]
    total = sum(t["duration_s"] or 0 for t in tracks)
    known = all(t["duration_s"] for t in tracks)
    claimed = parsed.get("claimed_count")
    return {"service": service, "name": parsed["name"], "tracks": tracks, "count": len(tracks),
            "total_s": total if known else None, "length_label": length_label(total) if known else "?",
            "folder_name": folder_name(parsed["name"], total if known else None),
            # the page showed fewer tracks than the playlist has (Apple loads the rest on scroll)
            "truncated": bool(claimed and len(tracks) < claimed), "claimed_count": claimed}


def fetch_playlist(url, urlopen=urllib.request.urlopen, spotify=None) -> dict:
    """Name, tracks in order, total length and folder name for a playlist link. `spotify(url)` reads a Spotify
    playlist (it needs the user's own login, so the app supplies it)."""
    service = service_of(url)
    if service == "apple":
        request = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urlopen(request, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except OSError as exc:
            raise PlaylistError(f"Could not open that Apple Music link ({type(exc).__name__}).") from exc
        return _finish("apple", parse_apple_page(html))
    if service == "spotify":
        if spotify is None:
            raise PlaylistError(SPOTIFY_UNAVAILABLE)
        return _finish("spotify", spotify(url))
    raise PlaylistError("Paste an Apple Music or Spotify playlist link.")


# ---- matching a track to a YouTube video -----------------------------------------------------
def choose_match(candidates, duration_s, tolerance=4.0):
    """The search result whose length is closest to the playlist track's (within `tolerance` seconds), else the
    first result. Different versions of a song are different videos, so the length decides between them."""
    candidates = [c for c in candidates if c]
    if not candidates:
        return None
    if duration_s:
        close = [c for c in candidates if c.get("length_s") and abs(c["length_s"] - duration_s) <= tolerance]
        if close:
            return min(close, key=lambda c: abs(c["length_s"] - duration_s))
    return candidates[0]


def find_video(yt, track):
    query = f"{track['artist']} {track['title']}"
    raw = yt.search(query, filter="songs", limit=5) or yt.search(query, limit=5)
    return choose_match([search.normalize_result(r) for r in raw or []], track["duration_s"])


# ---- the job ---------------------------------------------------------------------------------
class PlaylistJob:
    def __init__(self, result, root=None):
        self.id = uuid.uuid4().hex[:8]
        self.name = result["name"]
        self.tracks = result["tracks"]
        self.count = result["count"]
        self.folder = Path(root or playlist_dir()) / result["folder_name"]
        self.total = len(self.tracks)
        self.done = self.failed = self.linked = 0
        self.problems = []
        self.status = "running"
        self.started_at = time.time()
        self._lock = threading.Lock()

    def record(self, outcome, problem=None):
        with self._lock:
            if outcome == "failed":
                self.failed += 1
                if len(self.problems) < 10:
                    self.problems.append(problem)
            else:
                self.done += 1
                self.linked += outcome == "linked"

    def snapshot(self):
        return {"id": self.id, "name": self.name, "status": self.status, "total": self.total, "done": self.done,
                "failed": self.failed, "folder_name": self.folder.name, "problems": list(self.problems)}


def rip_track(conn, job, track, *, backend, yt):
    """One playlist track: find its video, make sure it is in the library, link it into the playlist folder.
    Returns "linked", "kept" (already there) or "failed"."""
    target = job.folder / track_filename(track["position"], job.count, track["artist"], track["title"])
    if target.exists():
        return "kept", None  # re-ripping: what is already in the folder is left alone
    video = find_video(yt, track)
    if not video:
        return "failed", f"{track['artist']} - {track['title']}: no match on YouTube Music"
    entry = rip.ensure_in_library(conn, backend, video_id=video["video_id"], artist=video["artist"],
                                  title=video["title"], album=video["album"] or track.get("album"),
                                  thumbnail=video["thumbnail"])
    files.place_link(entry["path"], job.folder, target.name)
    return "linked", None


def run_job(job, *, db_path, backend, yt_factory, concurrency=2, limit=None, on_progress=None):
    """Rip the playlist (blocking; run it in a thread). `limit` takes only the first N tracks (for tests)."""
    job.folder.mkdir(parents=True, exist_ok=True)
    tracks = job.tracks[:limit] if limit else job.tracks
    job.total = len(tracks)

    def one(track):
        conn = store.open_db(db_path)
        try:
            outcome, problem = rip_track(conn, job, track, backend=backend, yt=yt_factory())
        except Exception as exc:  # a download or disk problem on one track must not stop the playlist
            outcome, problem = "failed", f"{track['artist']} - {track['title']}: {rip.clean_error(exc)}"[:200]
        finally:
            conn.close()
        job.record(outcome, problem)
        if on_progress:
            on_progress(job)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, tracks))
    job.status = "done"
    if on_progress:
        on_progress(job)
    return job
