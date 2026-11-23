"""Pure helpers for search: recognise a pasted YouTube link and normalise YouTube Music results."""
import re
from urllib.parse import parse_qs, urlparse

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}


def video_id_from_text(text) -> str | None:
    """The video id in a pasted YouTube / YouTube Music link (watch, youtu.be, shorts, embed), else None.
    A bare 11-character word is NOT taken as an id: "Mississippi" is a search, not a video."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text and text.split("/")[0] in _HOSTS:
        text = "https://" + text
    try:
        url = urlparse(text)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or url.hostname not in _HOSTS:
        return None
    if url.hostname.endswith("youtu.be"):
        candidate = url.path.strip("/").split("/")[0]
    else:
        parts = [p for p in url.path.split("/") if p]
        if parts and parts[0] in ("shorts", "embed", "live", "v") and len(parts) > 1:
            candidate = parts[1]
        else:
            candidate = (parse_qs(url.query).get("v") or [""])[0]
    return candidate if _ID.match(candidate) else None


def _seconds(text):
    """'3:21' or '1:02:03' -> seconds, else None."""
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total if parts else None


def _artists(names):
    return ", ".join(n for n in names if n) or "Unknown Artist"


def normalize_result(r) -> dict | None:
    """One YouTube Music search result -> what the page needs, or None if it has no video id."""
    video_id = r.get("videoId")
    if not video_id:
        return None
    thumbs = r.get("thumbnails") or []
    seconds = r.get("duration_seconds")
    if seconds is None:
        seconds = _seconds(r.get("duration"))
    return {
        "video_id": video_id,
        "title": r.get("title") or "Untitled",
        "artist": _artists([a.get("name") for a in r.get("artists") or []]),
        "album": (r.get("album") or {}).get("name"),
        "length_s": seconds,
        "thumbnail": thumbs[-1].get("url") if thumbs else None,
    }


def normalize_song(info) -> dict | None:
    """`YTMusic.get_song()` (used for a pasted link) -> the same shape as a search result."""
    d = (info or {}).get("videoDetails") or {}
    video_id = d.get("videoId")
    if not video_id:
        return None
    thumbs = (d.get("thumbnail") or {}).get("thumbnails") or []
    try:
        seconds = float(d.get("lengthSeconds"))
    except (TypeError, ValueError):
        seconds = None
    artist = d.get("author") or "Unknown Artist"
    if artist.endswith(" - Topic"):  # YouTube's auto-generated artist channels
        artist = artist[: -len(" - Topic")]
    return {"video_id": video_id, "title": d.get("title") or "Untitled", "artist": artist, "album": None,
            "length_s": seconds, "thumbnail": thumbs[-1].get("url") if thumbs else None}
