"""Shared filename convention for ripped tracks: `Artist - Title`."""
import re

_ILLEGAL = re.compile(r'[<>:"/\|?*\x00-\x1f]')


def _clean(text: str) -> str:
    text = _ILLEGAL.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(". ")


def track_basename(artist: str, title: str) -> str:
    """`Artist - Title` without extension, safe on Windows/macOS/Linux."""
    artist, title = _clean(artist), _clean(title)
    if artist and title:
        return f"{artist} - {title}"
    return title or artist or "Unknown"


def ytdlp_outtmpl(download_dir: str, artist: str, title: str) -> str:
    """yt-dlp output template with the name fixed and `%` escaped."""
    name = track_basename(artist, title).replace("%", "%%")
    return f"{download_dir}/{name}.%(ext)s"
