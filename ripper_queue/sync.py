"""Keep tonight's queue in step with what Traktor loads (from traktor.state).

A track loaded onto any deck, even once, is PLAYED: it leaves the queue panel and is counted in "Played
tonight". Nothing happens to its file: it stays in tonight's folder, never removed, moved or renamed
because it was played. Unloading it later changes nothing; `played` is final.

A deck's file counts as an item's file when the paths match (case and slash style ignored) or when both
are the very same file on disk. The second case matters because tonight's file is a hard link: loading the
library copy or a playlist copy of the same track is the same file.
"""
import os

from . import files


def file_identity(path):
    """(device, file index) of an existing file, or None. Hard links of one file share it."""
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def compute_updates(items, decks, now, identity=file_identity):
    """{item_id: fields} for the items whose status or BPM should change. `items` are queue_items rows (dicts);
    `decks` is TraktorState.decks (or a copy of it)."""
    on_decks = [d for d in decks.values() if d["file_path"]]
    if not on_decks:
        return {}
    by_path = {}
    by_identity = {}
    for deck in on_decks:
        by_path.setdefault(files.normalize_path(deck["file_path"]), []).append(deck)
        ident = identity(deck["file_path"])
        if ident:
            by_identity.setdefault(ident, []).append(deck)
    updates = {}
    for item in items:
        if item["status"] not in ("ready", "played") or not item["file_path"]:
            continue  # nothing is in play for items that have no file yet, failed or were removed
        matched = by_path.get(files.normalize_path(item["file_path"]), [])
        if not matched:
            ident = identity(item["file_path"])
            matched = by_identity.get(ident, []) if ident else []
        if not matched:
            continue
        fields = {}
        if item["status"] == "ready":
            fields.update(status="played", played_at=now, loaded_at=now)
        bpm = next((d["bpm"] for d in matched if d["bpm"] and d["bpm"] > 0), None)
        if bpm and bpm != item["bpm"]:
            fields["bpm"] = bpm
        if fields:
            updates[item["id"]] = fields
    return updates


def apply_sync(conn, decks, now):
    """Run compute_updates against the database. Returns the ids of the items that changed."""
    rows = conn.execute("SELECT * FROM queue_items WHERE status IN ('ready', 'played') AND file_path IS NOT NULL").fetchall()
    updates = compute_updates([dict(r) for r in rows], decks, now)
    for item_id, fields in updates.items():
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE queue_items SET {cols} WHERE id = ?", (*fields.values(), item_id))
    if updates:
        conn.commit()
    return list(updates)
