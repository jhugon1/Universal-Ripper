"""File-side operations for queue items: naming with the order prefix, tagging, safe renames.

Rules (Traktor records the paths of loaded tracks in its collection, so files must stay put):
- nothing here deletes or moves an audio file;
- the only rename is changing the "NNN " order prefix of a queued/ready file, in place;
- loaded/played items are never renamed or retagged;
- every path touched must be inside the queue root.
"""
import os
import re
import urllib.request
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TIT2, TPE1

from filenames import track_basename
from . import store

_PREFIX = re.compile(r"^\d{3} ")
MAX_COVER_BYTES = 5 * 1024 * 1024


# ---- names -----------------------------------------------------------------------------------
def strip_marker(name: str) -> str:
    return _PREFIX.sub("", name, count=1)


def marker_name(position: int, name: str) -> str:
    """`001 Artist - Title.mp3`: three digits so the order survives past 99 items."""
    return f"{position:03d} {strip_marker(name)}"


def item_filename(position: int, artist: str, title: str) -> str:
    return marker_name(position, f"{track_basename(artist, title)}.mp3")


def is_inside(path, root) -> bool:
    """True if `path` resolves to somewhere under `root` (case-insensitive on Windows)."""
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives
        return False


def free_path(path: Path) -> Path:
    """`path`, or `name (2).ext`, `name (3).ext`... if something is already there."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ---- tags and length -------------------------------------------------------------------------
def detect_image(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None  # webp and the rest: not reliably shown by Traktor


def cover_url(url: str) -> str:
    """Ask Google's image host for a 600 px JPEG instead of its default (often webp)."""
    return re.sub(r"=w\d+-h\d+[^/]*$", "=w600-h600-l90-rj", url) if "googleusercontent.com" in url else url


def fetch_cover(url, timeout=10):
    """(bytes, mime) for a usable JPEG/PNG thumbnail, or None. Never raises."""
    try:
        with urllib.request.urlopen(urllib.request.Request(cover_url(url), headers={"User-Agent": "Mozilla/5.0"}),
                                    timeout=timeout) as resp:
            data = resp.read(MAX_COVER_BYTES + 1)
        mime = detect_image(data)
        return (data, mime) if mime and len(data) <= MAX_COVER_BYTES else None
    except Exception:
        return None


def tag_file(path, *, title, artist, album=None, cover=None):
    """Write title, artist, album and cover art as ID3v2.3. No track number: Traktor ignores it for order."""
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for frame in ("TIT2", "TPE1", "TALB", "APIC"):
        tags.delall(frame)
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if cover:
        data, mime = cover
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path, v2_version=3)


def read_length(path):
    """Length in seconds from the file itself, or None."""
    try:
        info = MutagenFile(path).info
        return float(info.length)
    except Exception:
        return None


# ---- the order prefix ------------------------------------------------------------------------
def apply_marker(conn, item_id, root) -> bool:
    """Make the file name carry the item's current position. Returns True when the name is right
    (or there is nothing to rename), False when it must be retried later: the item is locked or not
    a queued/ready item, the file is missing or outside the root, or another program holds it."""
    item = store.get_item(conn, item_id)
    if not item or item["status"] in store.LOCKED or item["status"] not in ("queued", "ready"):
        return False
    if not item["file_path"]:
        return True  # not downloaded yet: the prefix is written when the file lands
    path = Path(item["file_path"])
    if not is_inside(path, root) or not path.is_file():
        return False
    if item["marker_position"] == item["position"] and _PREFIX.match(path.name):
        return True
    new_name = marker_name(item["position"], path.name)
    if new_name != path.name:
        target = free_path(path.with_name(new_name))
        try:
            os.rename(path, target)  # in place: same folder, only the prefix changes
        except OSError:
            return False  # held open by another program (Traktor?): try again later
        path = target
    store.update_item(conn, item_id, file_path=str(path), marker_position=item["position"])
    return True


def sync_markers(conn, root) -> int:
    """Retry every queued/ready file whose prefix does not match its position. Returns how many
    files were renamed; the folder is marked changed if any were."""
    rows = conn.execute("SELECT id, marker_position, position FROM queue_items WHERE status IN ('queued', 'ready') "
                        "AND file_path IS NOT NULL AND (marker_position IS NULL OR marker_position != position)").fetchall()
    renamed = sum(1 for r in rows if apply_marker(conn, r["id"], root))
    if renamed:
        store.mark_folder_changed(conn)
    return renamed
