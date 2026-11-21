"""Keep queue items in step with what Traktor reports (from traktor.state).

- A deck loading an item's file (path match, ignoring case and slash style) makes it `loaded`.
- A loaded item that has been audible >= 30 s OR playing >= 90 s becomes `played`. The OR matters
  because the deck-to-channel mapping is unverified: if it were wrong, audible time would never
  accumulate, but playing time still does.
- A loaded item whose deck loads something else without having been played goes back to `ready`.
- `played` is final. The BPM of a loaded item is whatever Traktor reports for the deck.
"""
from . import files

PLAYED_AUDIBLE_SECONDS = 30.0
PLAYED_PLAYING_SECONDS = 90.0


def is_played(deck) -> bool:
    return deck["audible_seconds"] >= PLAYED_AUDIBLE_SECONDS or deck["playing_seconds"] >= PLAYED_PLAYING_SECONDS


def compute_updates(items, decks, now):
    """{item_id: fields} for the items whose status or BPM should change. Pure.
    `items` are queue_items rows (dicts); `decks` is TraktorState.decks (or a copy of it)."""
    by_path = {}
    for deck in decks.values():
        if deck["file_path"]:
            by_path.setdefault(files.normalize_path(deck["file_path"]), []).append(deck)
    updates = {}
    for item in items:
        if item["status"] not in ("ready", "loaded") or not item["file_path"]:
            continue  # queued/downloading/failed/removed have no file in play; played is final
        on_decks = by_path.get(files.normalize_path(item["file_path"]), [])
        fields = {}
        if on_decks:
            if item["status"] == "ready":
                fields.update(status="loaded", loaded_at=now)
            if any(is_played(d) for d in on_decks):
                fields.update(status="played", played_at=now, loaded_at=item["loaded_at"] or now)
            bpm = next((d["bpm"] for d in on_decks if d["bpm"] and d["bpm"] > 0), None)
            if bpm and bpm != item["bpm"]:
                fields["bpm"] = bpm
        elif item["status"] == "loaded":
            fields.update(status="ready", loaded_at=None)  # ejected before it was played
        if fields:
            updates[item["id"]] = fields
    return updates


def apply_sync(conn, decks, now):
    """Run compute_updates against the database. Returns the ids of the items that changed."""
    rows = conn.execute("SELECT * FROM queue_items WHERE status IN ('ready', 'loaded') AND file_path IS NOT NULL").fetchall()
    updates = compute_updates([dict(r) for r in rows], decks, now)
    for item_id, fields in updates.items():
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE queue_items SET {cols} WHERE id = ?", (*fields.values(), item_id))
    if updates:
        conn.commit()
    return list(updates)
