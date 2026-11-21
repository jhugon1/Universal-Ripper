"""Pure interpretation of the Traktor mod's events. No I/O; time comes from an injectable clock.

The mod (traktor-api-client) POSTs JSON to paths such as /deckLoaded/A, /updateDeck/A,
/updateChannel/1 and /updateMasterClock. `TraktorState.apply` folds one event into the state and
returns only the changes that actually happened (the mod sends duplicates).

Facts from the Traktor 4.0.2 capture: trackLength/elapsedTime are seconds, tempo is a ratio
(1 = unchanged), startup sends deckLoaded with an empty filePath for every deck and reports every
channel as on air.
"""
import time
from pathlib import PureWindowsPath

DECK_IDS = ("A", "B", "C", "D")
CHANNEL_IDS = ("1", "2", "3", "4")
# UNVERIFIED until the fader test: assumes deck N is on mixer channel N.
DECK_TO_CHANNEL = {"A": "1", "B": "2", "C": "3", "D": "4"}
AUDIBLE_LEVEL = 0.05
# The mod posts elapsedTime every second while a deck plays. Audible time is not credited across
# gaps longer than CREDIT_GAP. The display marks a playing deck "stale" after STALE_AFTER seconds
# of silence and shows it as paused after LOST_AFTER.
CREDIT_GAP = 3.0
STALE_AFTER = 5.0
LOST_AFTER = 60.0

_DECK_FIELDS = {
    "filePath": "file_path", "title": "title", "artist": "artist", "bpm": "bpm", "tempo": "tempo",
    "key": "key", "trackLength": "track_length", "elapsedTime": "elapsed", "isPlaying": "is_playing",
}
_STR_FIELDS = {"file_path", "title", "artist", "key"}
_NUM_FIELDS = {"bpm", "tempo", "track_length", "elapsed"}


def _clean_str(v):
    return v if isinstance(v, str) and v != "" else None


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _empty_deck():
    return {
        "file_path": None, "title": None, "artist": None, "bpm": None, "tempo": 1.0, "key": None,
        "track_length": None, "elapsed": 0.0, "elapsed_at": None, "is_playing": False,
        "loaded_at": None, "started_at": None, "audible_seconds": 0.0, "raw": {},
        "updated_at": None, "restored": False,
    }


def _empty_channel():
    return {"is_on_air": None, "on_air_level": None, "raw": {}}


def deck_status(deck, now):
    """What to show for a deck: empty, paused, playing, or stale (playing but the signal went quiet).
    A deck that has been quiet for LOST_AFTER seconds is shown as paused."""
    if not deck["file_path"]:
        return "empty"
    if not deck["is_playing"]:
        return "paused"
    age = None if deck["updated_at"] is None else now - deck["updated_at"]
    if age is None or age > LOST_AFTER:
        return "paused"
    return "stale" if age > STALE_AFTER else "playing"


def file_name_of(file_path):
    """Last path component. The mod sends Windows paths; '/' separators work too."""
    return PureWindowsPath(file_path).name if file_path else None


class TraktorState:
    def __init__(self, clock=time.time):
        self._clock = clock
        self.decks = {d: _empty_deck() for d in DECK_IDS}
        self.channels = {c: _empty_channel() for c in CHANNEL_IDS}
        self.master = {"deck": None, "bpm": None}
        self.last_event_at = None
        self.event_count = 0
        self.ignored_count = 0
        self.last_played = None
        self._last_tick = None

    # ---- derived -------------------------------------------------------------------------
    def is_audible(self, deck_id):
        d = self.decks[deck_id]
        ch = self.channels[DECK_TO_CHANNEL[deck_id]]
        level = ch["on_air_level"]
        return bool(
            d["file_path"] and d["is_playing"] and ch["is_on_air"] is True
            and level is not None and level > AUDIBLE_LEVEL
        )

    def now_playing(self):
        """Audible deck with the highest on-air level; ties go to the most recently started."""
        best = None
        for deck_id in DECK_IDS:
            if not self.is_audible(deck_id):
                continue
            d = self.decks[deck_id]
            rank = (round(self.channels[DECK_TO_CHANNEL[deck_id]]["on_air_level"], 2), d["started_at"] or 0.0)
            if best is None or rank > best[0]:
                best = (rank, deck_id)
        return best[1] if best else None

    def _tick(self, now):
        """Credit audible time since the last tick, unless the data is stale."""
        last, self._last_tick = self._last_tick, now
        if last is None or now <= last:
            return
        if self.last_event_at is None or now - self.last_event_at > CREDIT_GAP:
            return
        for deck_id in DECK_IDS:
            if self.is_audible(deck_id):
                self.decks[deck_id]["audible_seconds"] += now - last

    # ---- applying events -----------------------------------------------------------------
    def apply(self, path, body, now=None):
        """Fold one event into the state. Returns the list of real changes (possibly empty)."""
        now = self._clock() if now is None else now
        parts = [p for p in str(path).split("/") if p]
        if not parts or not isinstance(body, dict):
            self.ignored_count += 1
            return []
        kind, arg = parts[0], (parts[1] if len(parts) > 1 else "")
        self._tick(now)
        before = self.now_playing()
        changes = []
        touched = None
        if kind in ("deckLoaded", "updateDeck") and arg.upper() in DECK_IDS:
            deck_id = touched = arg.upper()
            if kind == "deckLoaded":
                self._deck_loaded(deck_id, body, now, changes)
            else:
                self._apply_deck_fields(deck_id, body, now, changes)
        elif kind == "updateChannel" and arg in CHANNEL_IDS:
            self._apply_channel(arg, body, changes)
        elif kind == "updateMasterClock":
            self._apply_master(body, changes)
        else:
            self.ignored_count += 1
            return changes
        if touched:  # any fresh event replaces restored data and shows the deck is alive
            self.decks[touched]["updated_at"] = now
            self.decks[touched]["restored"] = False
        self.last_event_at = now
        self.event_count += 1
        after = self.now_playing()
        if after != before:
            changes.append({"entity": "derived", "id": "", "field": "now_playing", "old": before, "new": after})
        if after is not None:
            d = self.decks[after]
            self.last_played = {
                "deck": after, "title": d["title"], "artist": d["artist"], "file_name": file_name_of(d["file_path"]),
            }
        return changes

    @staticmethod
    def _set(store, entity, entity_id, field, value, changes):
        if store[field] != value:
            changes.append({"entity": entity, "id": entity_id, "field": field, "old": store[field], "new": value})
            store[field] = value

    def _deck_loaded(self, deck_id, body, now, changes):
        d = self.decks[deck_id]
        new_path = _clean_str(body.get("filePath"))
        if new_path is None:  # the startup burst sends this for every deck: the deck is empty
            fresh = _empty_deck()
            for field, value in fresh.items():
                if field not in ("raw", "updated_at", "restored"):
                    self._set(d, "deck", deck_id, field, value, changes)
            d["raw"] = {}
            return
        if new_path != d["file_path"]:  # a different track: start its bookkeeping from zero
            self._set(d, "deck", deck_id, "loaded_at", now, changes)
            self._set(d, "deck", deck_id, "audible_seconds", 0.0, changes)
            self._set(d, "deck", deck_id, "started_at", None, changes)
            self._set(d, "deck", deck_id, "is_playing", False, changes)
        self._apply_deck_fields(deck_id, body, now, changes)

    def _apply_deck_fields(self, deck_id, body, now, changes):
        d = self.decks[deck_id]
        for key, value in body.items():
            field = _DECK_FIELDS.get(key)
            if field is None:
                d["raw"][key] = value
                continue
            if field in _STR_FIELDS:
                value = _clean_str(value)
            elif field in _NUM_FIELDS:
                value = _num(value)
                if value is None or (field == "tempo" and value <= 0):
                    continue
            elif not isinstance(value, bool):
                continue
            was_playing = d["is_playing"]
            if field == "elapsed":
                d["elapsed_at"] = now
            self._set(d, "deck", deck_id, field, value, changes)
            if field == "is_playing" and value != was_playing:
                self._set(d, "deck", deck_id, "started_at", now if value else None, changes)

    def _apply_channel(self, channel_id, body, changes):
        ch = self.channels[channel_id]
        for key, value in body.items():
            if key == "isOnAir" and isinstance(value, bool):
                self._set(ch, "channel", channel_id, "is_on_air", value, changes)
            elif key == "onAirLevel" and _num(value) is not None:
                self._set(ch, "channel", channel_id, "on_air_level", _num(value), changes)
            else:
                ch["raw"][key] = value

    def _apply_master(self, body, changes):
        if "deck" in body:
            deck = body["deck"].upper() if isinstance(body["deck"], str) and body["deck"].upper() in DECK_IDS else None
            self._set(self.master, "master", "", "deck", deck, changes)
        if "bpm" in body:
            self._set(self.master, "master", "", "bpm", _num(body["bpm"]), changes)

    # ---- output --------------------------------------------------------------------------
    def snapshot(self, now=None, include_path=False):
        """JSON-able view of everything. The full file path is left out unless asked for."""
        now = self._clock() if now is None else now
        self._tick(now)
        decks = {}
        for deck_id in DECK_IDS:
            d = dict(self.decks[deck_id])
            d["raw"] = dict(d["raw"])
            d["file_name"] = file_name_of(d["file_path"])
            d["audible"] = self.is_audible(deck_id)
            d["status"] = deck_status(self.decks[deck_id], now)
            if not include_path:
                del d["file_path"]
            decks[deck_id] = d
        return {
            "server_time": now,
            "now_playing": self.now_playing(),
            "last_played": self.last_played,
            "decks": decks,
            "channels": {c: dict(v, raw=dict(v["raw"])) for c, v in self.channels.items()},
            "master": dict(self.master),
            "seconds_since_event": None if self.last_event_at is None else now - self.last_event_at,
            "event_count": self.event_count,
            "stale_after": STALE_AFTER,
            "lost_after": LOST_AFTER,
        }

    # ---- persistence (restart recovery) --------------------------------------------------
    def dump(self):
        """Everything needed to restore after a restart. Includes file paths: keep it in the local DB only."""
        return {
            "decks": {k: {f: v for f, v in d.items() if f != "raw"} for k, d in self.decks.items()},
            "channels": {k: {f: v for f, v in c.items() if f != "raw"} for k, c in self.channels.items()},
            "master": dict(self.master),
            "last_played": self.last_played,
        }

    def load(self, data):
        """Restore a dump(). Decks with a track are marked restored until a fresh event replaces them."""
        blank = _empty_deck()
        for deck_id, saved in (data.get("decks") or {}).items():
            if deck_id not in self.decks or not isinstance(saved, dict):
                continue
            d = self.decks[deck_id]
            for field in blank:
                if field == "raw" or field not in saved:
                    continue
                value = saved[field]
                if field in _STR_FIELDS:
                    value = _clean_str(value)
                elif field == "is_playing":
                    value = value is True
                elif field == "restored":
                    continue
                else:
                    value = _num(value)
                    if value is None and field in ("tempo", "elapsed", "audible_seconds"):
                        value = blank[field]
                d[field] = value
            d["restored"] = bool(d["file_path"])
        for channel_id, saved in (data.get("channels") or {}).items():
            if channel_id in self.channels and isinstance(saved, dict):
                ch = self.channels[channel_id]
                ch["is_on_air"] = saved.get("is_on_air") if isinstance(saved.get("is_on_air"), bool) else None
                ch["on_air_level"] = _num(saved.get("on_air_level"))
        master = data.get("master")
        if isinstance(master, dict):
            self.master = {"deck": master.get("deck") if master.get("deck") in DECK_IDS else None,
                           "bpm": _num(master.get("bpm"))}
        lp = data.get("last_played")
        self.last_played = lp if isinstance(lp, dict) else None
