"""The library: every rip ever made, one file per YouTube video id.

Files live in one folder (`RIPPER_LIBRARY_DIR`, default Music/Traktor/DJ_Search_Rips) as
`Artist - Title.mp3`, tagged with the YouTube video id (TXXX:YT_VIDEO_ID) so the table can always be
rebuilt from the files. A "duplicate" means the same video id and nothing else: other versions of a
song are different videos and are different rips.

Nothing here deletes, moves or renames an audio file. The table only describes what is in the folder.
"""
import os
import shutil
import time
from pathlib import Path

from . import files, store

MIGRATED_FLAG = "library_migrated"


def library_dir() -> Path:
    env = os.environ.get("RIPPER_LIBRARY_DIR")
    return Path(env) if env else Path.home() / "Music" / "Traktor" / "DJ_Search_Rips"


def _row(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def get(conn, library_id):
    return _row(conn, "SELECT * FROM library WHERE id = ?", (library_id,))


def find(conn, video_id):
    """The library entry for this YouTube video id, or None. An entry whose file has been deleted by hand
    is forgotten (the table only describes the folder), so that video can be ripped again."""
    if not video_id:
        return None
    row = _row(conn, "SELECT * FROM library WHERE video_id = ?", (video_id,))
    if row and not Path(row["path"]).is_file():
        conn.execute("DELETE FROM library WHERE id = ?", (row["id"],))
        conn.commit()
        return None
    return row


def known_ids(conn, video_ids):
    """Which of these video ids are in the library (for the "In library" note on search results)."""
    ids = [v for v in video_ids if v]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT video_id, path FROM library WHERE video_id IN ({marks})", ids).fetchall()
    return {r["video_id"] for r in rows if Path(r["path"]).is_file()}


def add(conn, *, video_id, artist, title, album, length_s, path, source="rip", now=None):
    """Record a file that now exists in the library folder. Returns the entry."""
    now = time.time() if now is None else now
    mtime = Path(path).stat().st_mtime_ns
    existing = _row(conn, "SELECT id FROM library WHERE path = ?", (str(path),))
    if existing:
        conn.execute("UPDATE library SET video_id = ?, artist = ?, title = ?, album = ?, length_s = ?, mtime_ns = ? WHERE id = ?",
                     (video_id, artist, title, album, length_s, mtime, existing["id"]))
        library_id = existing["id"]
    else:
        cur = conn.execute("INSERT INTO library (video_id, artist, title, album, length_s, path, mtime_ns, source, ripped_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (video_id, artist, title, album, length_s, str(path), mtime, source, now))
        library_id = cur.lastrowid
    conn.commit()
    return get(conn, library_id)


def reindex(conn, directory=None) -> dict:
    """Make the table match the files in the library folder (tags are the source of truth). Files that have
    not changed since the last scan are skipped. Returns counts."""
    directory = Path(directory or library_dir())
    counts = {"added": 0, "updated": 0, "removed": 0}
    on_disk = {}
    if directory.is_dir():
        for p in sorted(directory.iterdir()):
            if p.suffix.lower() == ".mp3" and p.is_file():
                on_disk[str(p)] = p
    rows = {r["path"]: dict(r) for r in conn.execute("SELECT * FROM library")}
    for path, p in on_disk.items():
        mtime = p.stat().st_mtime_ns
        row = rows.get(path)
        if row and row["mtime_ns"] == mtime:
            continue
        tags = files.read_tags(p)
        video_id = tags["video_id"]
        owner = _row(conn, "SELECT path FROM library WHERE video_id = ?", (video_id,)) if video_id else None
        if owner and owner["path"] != path:  # a second file for the same video: the first one keeps the id
            video_id = None
        add(conn, video_id=video_id, artist=tags["artist"], title=tags["title"], album=tags["album"],
            length_s=tags["length_s"], path=path, source=row["source"] if row else "indexed")
        counts["updated" if row else "added"] += 1
    for path, row in rows.items():
        if path not in on_disk:  # the file is gone: forget the row (never touches any file)
            conn.execute("DELETE FROM library WHERE id = ?", (row["id"],))
            counts["removed"] += 1
    conn.commit()
    return counts


def migrate_old_rips(old_dir, directory=None) -> int:
    """COPY the old standalone rips into the library (originals stay where they are). Files already there
    by name are left alone. Returns how many were copied."""
    old_dir, directory = Path(old_dir), Path(directory or library_dir())
    if not old_dir.is_dir():
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in sorted(old_dir.iterdir()):
        if p.suffix.lower() == ".mp3" and p.is_file() and not (directory / p.name).exists():
            shutil.copy2(p, directory / p.name)
            copied += 1
    return copied


def ensure_ready(conn, old_dir=None, migrate=True) -> dict:
    """Start-up: make sure the folder exists, copy the old rips in once (first run only), and index the folder."""
    library_dir().mkdir(parents=True, exist_ok=True)
    copied = 0
    if migrate and old_dir and not store.meta_get(conn, MIGRATED_FLAG):
        copied = migrate_old_rips(old_dir)
        store.meta_set(conn, MIGRATED_FLAG, str(time.time()))
    counts = reindex(conn)
    counts["migrated"] = copied
    return counts
