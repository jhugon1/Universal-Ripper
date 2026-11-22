"""Storage and ordering for the Ripper Queue: nightly sessions, queue items, positions.

Pure SQLite, no network and no file operations here. Order is a plain integer `position` per
session; it is written into the file's name as a prefix by ripper_queue.files.
"""
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_PLAYLIST = "Ripper Queue"
# Items that may change position, and whose file (if it exists yet) may be renamed.
MOVABLE = ("queued", "downloading", "ready")
# Traktor records the paths of loaded tracks in its collection: never rename these.
LOCKED = ("loaded", "played")  # ("loaded" is no longer used: loading a track makes it "played")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions
       (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        started_at REAL NOT NULL, ended_at REAL)""",
    """CREATE TABLE IF NOT EXISTS queue_items
       (id INTEGER PRIMARY KEY AUTOINCREMENT, playlist TEXT NOT NULL DEFAULT 'Ripper Queue',
        session_id INTEGER NOT NULL REFERENCES sessions(id), position INTEGER NOT NULL,
        source TEXT NOT NULL, request_id INTEGER, query TEXT, artist TEXT, title TEXT, video_id TEXT,
        status TEXT NOT NULL DEFAULT 'queued', file_path TEXT, length_s REAL, bpm REAL, error TEXT,
        added_at REAL NOT NULL, loaded_at REAL, played_at REAL,
        marker_position INTEGER, album TEXT, thumbnail TEXT, library_id INTEGER)""",  # marker_position: the prefix currently on the file name
    "CREATE INDEX IF NOT EXISTS queue_items_session ON queue_items (session_id, position)",
    "CREATE TABLE IF NOT EXISTS queue_meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS library
       (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT, artist TEXT, title TEXT, album TEXT, length_s REAL,
        path TEXT NOT NULL UNIQUE, mtime_ns INTEGER, source TEXT NOT NULL DEFAULT 'rip', ripped_at REAL NOT NULL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS library_video ON library (video_id) WHERE video_id IS NOT NULL",
)

# columns added after the first version of queue_items: older databases get them on start-up
_ADDED_COLUMNS = (("album", "TEXT"), ("thumbnail", "TEXT"), ("library_id", "INTEGER"))


def queue_dir() -> Path:
    """Root folder Traktor browses. RIPPER_QUEUE_DIR overrides the default."""
    env = os.environ.get("RIPPER_QUEUE_DIR")
    return Path(env) if env else Path.home() / "Music" / "Traktor" / "Ripper Queue"


def open_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    have = {row[1] for row in conn.execute("PRAGMA table_info(queue_items)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE queue_items ADD COLUMN {name} {kind}")
    conn.commit()


# ---- sessions --------------------------------------------------------------------------------
def _unique_session_name(conn, now):
    base = time.strftime("%Y-%m-%d", time.localtime(now))
    name, n = base, 1
    while conn.execute("SELECT 1 FROM sessions WHERE name = ?", (name,)).fetchone():
        n += 1
        name = f"{base}-{n}"
    return name


def _start_session(conn, now):
    cur = conn.execute("INSERT INTO sessions (name, started_at) VALUES (?, ?)", (_unique_session_name(conn, now), now))
    conn.commit()
    return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone())


def current_session(conn, now=None) -> dict:
    """The latest session that has not ended; one is created on first use."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else _start_session(conn, now)


def new_night(conn, now=None) -> dict:
    """End the current session and start a new one. Does nothing if the current one is still empty."""
    now = time.time() if now is None else now
    cur = current_session(conn, now)
    if not conn.execute("SELECT 1 FROM queue_items WHERE session_id = ?", (cur["id"],)).fetchone():
        return cur
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (now, cur["id"]))
    conn.commit()
    return _start_session(conn, now)


def session_dir(session: dict, root=None) -> Path:
    return Path(root or queue_dir()) / session["name"]


# ---- items -----------------------------------------------------------------------------------
def get_item(conn, item_id):
    row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_items(conn, session_id, include_removed=False):
    sql = "SELECT * FROM queue_items WHERE session_id = ?" + ("" if include_removed else " AND status != 'removed'")
    return [dict(r) for r in conn.execute(sql + " ORDER BY position, id", (session_id,))]


def add_item(conn, *, source, artist, title, query=None, video_id=None, album=None, thumbnail=None, now=None):
    """Append an item to the current session. Returns (item, True)."""
    now = time.time() if now is None else now
    session = current_session(conn, now)
    position = (conn.execute("SELECT COALESCE(MAX(position), 0) FROM queue_items WHERE session_id = ?",
                             (session["id"],)).fetchone()[0]) + 1
    cur = conn.execute(
        "INSERT INTO queue_items (playlist, session_id, position, source, query, artist, title, video_id, "
        "album, thumbnail, status, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
        (DEFAULT_PLAYLIST, session["id"], position, source, query, artist, title, video_id,
         album, thumbnail, now))
    conn.commit()
    return get_item(conn, cur.lastrowid), True


def split_queue(items):
    """(queue, played) from ONE list of items, so a track that is marked played at this very moment is in exactly
    one of them. The queue is what is still to play (plus failed rips) in order; played is in the order the
    tracks were loaded."""
    queue = [i for i in items if i["status"] != "played"]
    played = sorted((i for i in items if i["status"] == "played"), key=lambda i: (i["played_at"] or 0, i["id"]))
    return queue, played


def update_item(conn, item_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE queue_items SET {cols} WHERE id = ?", (*fields.values(), item_id))
    conn.commit()


def claim_next(conn):
    """Atomically take the next queued item (lowest session, then position) and mark it downloading."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM queue_items WHERE status = 'queued' ORDER BY session_id, position, id LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE queue_items SET status = 'downloading', error = NULL WHERE id = ?", (row["id"],))
        return dict(row, status="downloading")
    finally:
        conn.commit()


def reset_stale_downloads(conn):
    """After a crash or restart nothing is really downloading: put those items back in the queue."""
    conn.execute("UPDATE queue_items SET status = 'queued' WHERE status = 'downloading'")
    conn.commit()


# ---- ordering --------------------------------------------------------------------------------
def plan_move(items, item_id, direction):
    """New positions {id: position} for moving one item up or down among the MOVABLE items of its
    session, or {} if it cannot move. Loaded/played/failed/removed items keep their positions."""
    movable = sorted((i for i in items if i["status"] in MOVABLE), key=lambda i: (i["position"], i["id"]))
    idx = next((k for k, i in enumerate(movable) if i["id"] == item_id), None)
    if idx is None:
        return {}
    other = idx - 1 if direction == "up" else idx + 1 if direction == "down" else -1
    if other < 0 or other >= len(movable):
        return {}
    a, b = movable[idx], movable[other]
    return {a["id"]: b["position"], b["id"]: a["position"]}


def move_item(conn, item_id, direction):
    """Swap an item with its movable neighbour. Returns the ids whose position changed."""
    item = get_item(conn, item_id)
    if not item:
        return []
    plan = plan_move(list_items(conn, item["session_id"]), item_id, direction)
    for pid, position in plan.items():
        conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (position, pid))
    conn.commit()
    return list(plan)


def meta_get(conn, key):
    row = conn.execute("SELECT value FROM queue_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO queue_meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


# ---- the "re-select the folder in Traktor" hint ------------------------------------------------
def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM queue_meta WHERE key = ?", (key,)).fetchone()
    return float(row[0]) if row else 0.0


def _meta_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO queue_meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def mark_folder_changed(conn, now=None):
    """Files appeared or were renamed in the queue folder: Traktor needs a re-select."""
    _meta_set(conn, "folder_changed_at", time.time() if now is None else now)


def ack_folder(conn, now=None):
    _meta_set(conn, "folder_ack_at", time.time() if now is None else now)


def folder_state(conn):
    changed, ack = _meta_get(conn, "folder_changed_at"), _meta_get(conn, "folder_ack_at")
    return {"changed_at": changed or None, "ack_at": ack or None, "reselect_needed": changed > ack}
