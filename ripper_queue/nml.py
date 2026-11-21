"""Read-only access to Traktor's collection.nml, for the analyzed BPM of tracks.

Traktor writes the collection when it saves (on exit and now and then), and only tracks it has
imported/analyzed are in it, so values appear late. This module never writes to Traktor's folder.
"""
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from . import files

_cache = {}  # collection path -> (mtime_ns, size, index)


def version_key(name: str):
    return tuple(int(n) for n in re.findall(r"\d+", name))


def traktor_dir():
    """Traktor's user data folder. RIPPER_TRAKTOR_DIR overrides; otherwise the newest
    `Documents/Native Instruments/Traktor <version>` that has a collection.nml."""
    env = os.environ.get("RIPPER_TRAKTOR_DIR")
    if env:
        return Path(env)
    base = Path.home() / "Documents" / "Native Instruments"
    found = [p for p in base.glob("Traktor *") if (p / "collection.nml").is_file()] if base.is_dir() else []
    return max(found, key=lambda p: version_key(p.name)) if found else None


def collection_path():
    d = traktor_dir()
    return d / "collection.nml" if d and (d / "collection.nml").is_file() else None


def entry_path(volume, directory, file):
    """Rebuild a file path from NML's LOCATION: DIR="/:Users/:me/:Music/:", VOLUME="C:", FILE="x.mp3"."""
    if volume and re.fullmatch(r"[A-Za-z]:", volume):
        return volume + (directory or "").replace("/:", "\\") + (file or "")
    return (directory or "").replace("/:", "/") + (file or "")


def bpm_index(path):
    """{normalized file path: BPM} for every analyzed track in the collection. Cached by mtime and size.
    A file that cannot be read or parsed right now (Traktor may be writing it) gives {} and is retried."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    index = {}
    try:
        for _, elem in ET.iterparse(path, events=("end",)):  # read-only, streamed: collections can be large
            if elem.tag != "ENTRY":
                continue
            loc, tempo = elem.find("LOCATION"), elem.find("TEMPO")
            if loc is not None and tempo is not None:
                try:
                    bpm = float(tempo.get("BPM") or 0)
                except ValueError:
                    bpm = 0.0
                if bpm > 0:
                    index[files.normalize_path(entry_path(loc.get("VOLUME"), loc.get("DIR"), loc.get("FILE")))] = bpm
            elem.clear()
    except (ET.ParseError, OSError):
        return {}
    _cache[str(path)] = (key, index)
    return index


def fill_bpm(conn, index) -> int:
    """Give items that have no BPM yet the analyzed value from the collection. Returns how many were set."""
    rows = conn.execute("SELECT id, file_path FROM queue_items WHERE bpm IS NULL AND file_path IS NOT NULL "
                        "AND status IN ('ready', 'loaded', 'played')").fetchall()
    count = 0
    for row in rows:
        bpm = index.get(files.normalize_path(row["file_path"]))
        if bpm:
            conn.execute("UPDATE queue_items SET bpm = ? WHERE id = ?", (round(bpm, 2), row["id"]))
            count += 1
    if count:
        conn.commit()
    return count
