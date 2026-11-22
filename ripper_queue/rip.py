"""What pressing Rip means.

- The same video is already live in tonight's queue  -> "in_queue": nothing changes.
- The video is already in the library                -> "duplicate": never downloaded again; a link to the
  library file is added to tonight's folder at once.
- Otherwise                                          -> "new": a queue item is created and the worker
  downloads it into the library, then links it into tonight's folder.

A hard link is used so the same file serves every folder (the copy fallback is in files.place_link).
"""
import time
from pathlib import Path

from . import files, library, store

LIVE = ("queued", "downloading", "ready")


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
