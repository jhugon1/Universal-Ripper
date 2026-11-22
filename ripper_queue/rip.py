"""What pressing Rip means.

- The same video is already live in tonight's queue  -> "in_queue": nothing changes.
- The video is already in the library                -> "duplicate": never downloaded again; a link to the
  library file is added to tonight's folder at once.
- Otherwise                                          -> "new": a queue item is created and the worker
  downloads it into the library, then links it into tonight's folder.

A hard link is used so the same file serves every folder (the copy fallback is in files.place_link).
"""
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from filenames import track_basename

from . import files, library, store, sync

LIVE = ("queued", "downloading", "ready")

_locks = {}
_locks_guard = threading.Lock()


def video_lock(video_id):
    """One lock per video id, so the same video is never downloaded twice at the same time (two playlist tracks
    that resolve to one video, or a playlist and a search rip)."""
    with _locks_guard:
        return _locks.setdefault(video_id, threading.Lock())


def ensure_in_library(conn, backend, *, video_id, artist, title, album=None, thumbnail=None):
    """The library entry for this video: found, or downloaded ONCE into the library as `Artist - Title.mp3`,
    tagged (title, artist, album, cover, video id)."""
    with video_lock(video_id):
        entry = library.find(conn, video_id)
        if entry:
            return entry
        tmp = Path(tempfile.mkdtemp(prefix="ripper_queue_"))
        try:
            audio = Path(backend.download(video_id, tmp))
            cover = files.fetch_cover(thumbnail) if thumbnail else None
            files.tag_file(audio, title=title, artist=artist, album=album, cover=cover, video_id=video_id)
            length = files.read_length(audio)
            folder = library.library_dir()
            folder.mkdir(parents=True, exist_ok=True)
            target = files.free_path(folder / f"{track_basename(artist, title)}.mp3")
            part = target.with_name(target.name + ".part")  # Traktor ignores it until the rename below
            shutil.copyfile(audio, part)
            os.replace(part, target)
            return library.add(conn, video_id=video_id, artist=artist, title=title, album=album, length_s=length,
                               path=target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)  # our own temp folder, never the library or the queue folder


def live_item(conn, session_id, video_id):
    row = conn.execute("SELECT * FROM queue_items WHERE session_id = ? AND video_id = ? AND status IN "
                       "('queued', 'downloading', 'ready') ORDER BY id LIMIT 1", (session_id, video_id)).fetchone()
    return dict(row) if row else None


def link_item(conn, item, entry, root=None):
    """Put a link to the library file into tonight's folder as `NNN Artist - Title.mp3` and mark the item ready."""
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (item["session_id"],)).fetchone()
    folder = store.session_dir(dict(session), root)
    target, _kind = files.place_link(entry["path"], folder, files.item_filename(item["position"], item["artist"], item["title"]))
    store.update_item(conn, item["id"], status="ready", file_path=str(target), library_id=entry["id"],
                      length_s=entry["length_s"], marker_position=item["position"], error=None)
    store.mark_folder_changed(conn)
    return store.get_item(conn, item["id"])


def request_rip(conn, *, video_id, artist, title, album=None, thumbnail=None, root=None, now=None) -> dict:
    """Decide and start a rip. Returns {"kind": "in_queue" | "duplicate" | "new" | "error", "item": item, ...}."""
    now = time.time() if now is None else now
    session = store.current_session(conn, now)
    existing = live_item(conn, session["id"], video_id)
    if existing:
        return {"kind": "in_queue", "item": existing}
    item, _ = store.add_item(conn, source="rip", artist=artist, title=title, video_id=video_id, album=album,
                             thumbnail=thumbnail, query=f"{artist} {title}", now=now)
    entry = library.find(conn, video_id)
    if not entry:
        return {"kind": "new", "item": item}
    try:
        return {"kind": "duplicate", "item": link_item(conn, item, entry, root)}
    except OSError as exc:  # neither a link nor a copy could be made
        store.update_item(conn, item["id"], status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        return {"kind": "error", "item": store.get_item(conn, item["id"]), "message": str(exc)}


def remove_from_queue(conn, item_id, decks, queue_root, now=None) -> dict:
    """Take an item out of tonight's queue. If it was never loaded, its link in tonight's folder is deleted too
    (the library file is never touched: for a hard link only the extra name goes away). Played items are never
    touched. A track loaded onto a deck a moment ago, before the sync has noticed, is marked played instead of
    being removed. Returns {"error": ...} or {"result": "removed" | "kept" | "unchanged", ...}."""
    now = time.time() if now is None else now
    item = store.get_item(conn, item_id)
    if not item:
        return {"error": "no such item"}
    if item["status"] == "removed":
        return {"result": "unchanged"}
    if item["status"] in store.LOCKED:
        return {"result": "kept", "reason": "played"}
    updates = sync.compute_updates([item], decks, now)
    if item_id in updates:  # it is on a deck right now
        store.update_item(conn, item_id, **updates[item_id])
        return {"result": "kept", "reason": "loaded"}
    store.update_item(conn, item_id, status="removed")
    out = {"result": "removed", "deleted": False}
    if item["status"] == "ready" and item["file_path"]:
        out["deleted"] = _delete_link(conn, item, queue_root)
    return out


def _delete_link(conn, item, queue_root) -> bool:
    """Delete this item's own link (or copy) in tonight's folder. Only ever a file inside the queue folder that is
    not the library file itself. A failure is noted on the item and nothing else changes."""
    path = Path(item["file_path"])
    entry = library.get(conn, item["library_id"]) if item["library_id"] else None
    if (not files.is_inside(path, queue_root) or files.is_inside(path, library.library_dir())
            or (entry and files.normalize_path(entry["path"]) == files.normalize_path(path))):
        return False
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:  # for example Traktor has it open: leave it, say so
        store.update_item(conn, item["id"], error=f"Could not delete the file: {type(exc).__name__}")
        return False
    store.mark_folder_changed(conn)
    return True
