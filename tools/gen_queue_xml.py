#!/usr/bin/env python3
"""tools/gen_queue_xml.py - write an iTunes-style library XML holding ONE playlist, for Traktor's iTunes node.
  python tools/gen_queue_xml.py                      newest 3 audio files under ~/Music/Traktor -> 'Ripper Queue'
  python tools/gen_queue_xml.py --newest 5
  python tools/gen_queue_xml.py "C:\\path\\a.mp3" "C:\\path\\b.mp3"   explicit files, in this order"""
import argparse, datetime, hashlib, os, plistlib, re, sys
from pathlib import Path

AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif"}
KIND = {".mp3": "MPEG audio file", ".wav": "WAV audio file", ".m4a": "AAC audio file", ".aiff": "AIFF audio file", ".aif": "AIFF audio file", ".flac": "FLAC audio file"}
HOME = Path(os.environ.get("USERPROFILE") or Path.home())


def pid(s): return hashlib.md5(s.encode()).hexdigest()[:16].upper()
def uri(p): return p.resolve().as_uri().replace("file:///", "file://localhost/", 1)


def meta(p):
    m = {"name": p.stem, "artist": "", "album": "", "genre": "", "ms": None, "kbps": None, "hz": None}
    if " - " in p.stem:
        m["artist"], m["name"] = (x.strip() for x in p.stem.split(" - ", 1))
    try:
        import mutagen
        f = mutagen.File(p, easy=True)
        if f is not None:
            for k, key in (("name", "title"), ("artist", "artist"), ("album", "album"), ("genre", "genre")):
                if f.get(key): m[k] = str(f[key][0])
            if f.info and getattr(f.info, "length", None): m["ms"] = int(f.info.length * 1000)
            if f.info and getattr(f.info, "bitrate", None): m["kbps"] = int(f.info.bitrate // 1000)
            if f.info and getattr(f.info, "sample_rate", None): m["hz"] = int(f.info.sample_rate)
    except Exception:
        pass
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--source", default=str(HOME / "Music" / "Traktor"))
    ap.add_argument("--newest", type=int, default=3)
    ap.add_argument("--name", default="Ripper Queue")
    ap.add_argument("--out", default=str(HOME / "Music" / "Traktor" / "Ripper Queue" / "ripper_queue.xml"))
    a = ap.parse_args()

    if a.files:
        files = [Path(f) for f in a.files]
    else:
        pool = [p for p in Path(a.source).rglob("*") if p.suffix.lower() in AUDIO]
        files = sorted(sorted(pool, key=lambda p: p.stat().st_mtime, reverse=True)[:a.newest], key=lambda p: p.stat().st_mtime)
    files = [f for f in files if f.is_file()]
    if not files:
        sys.exit(f"no audio files found (looked in {a.source}). Pass file paths explicitly.")

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    tracks, items = {}, []
    for i, f in enumerate(files, 1):
        m, tid = meta(f), 1000 + i
        mod = datetime.datetime.fromtimestamp(f.stat().st_mtime, datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
        t = {"Track ID": tid, "Size": f.stat().st_size, "Total Time": m["ms"], "Date Modified": mod, "Date Added": now,
             "Bit Rate": m["kbps"], "Sample Rate": m["hz"], "Persistent ID": pid(str(f)), "Track Type": "File",
             "File Folder Count": -1, "Library Folder Count": -1, "Name": m["name"], "Artist": m["artist"],
             "Album": m["album"], "Genre": m["genre"], "Kind": KIND.get(f.suffix.lower(), "Audio file"), "Location": uri(f)}
        tracks[str(tid)] = {k: v for k, v in t.items() if v not in ("", None)}
        items.append({"Track ID": tid})
    lib = {"Major Version": 1, "Minor Version": 1, "Date": now, "Application Version": "12.13.2.3", "Features": 5,
           "Show Content Ratings": True, "Music Folder": uri(Path(a.source)) + "/", "Library Persistent ID": pid("ripper-library"),
           "Tracks": tracks,
           "Playlists": [
               {"Name": "Library", "Description": "", "Master": True, "Playlist ID": 100, "Playlist Persistent ID": pid("ripper-master"),
                "Visible": False, "All Items": True, "Playlist Items": list(items)},
               {"Name": a.name, "Description": "", "Playlist ID": 1000, "Playlist Persistent ID": pid(a.name), "All Items": True,
                "Playlist Items": items}]}

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fp:
        plistlib.dump(lib, fp, fmt=plistlib.FMT_XML, sort_keys=False)
    back = plistlib.loads(out.read_bytes())  # round-trip check
    ids = {t["Track ID"] for t in back["Tracks"].values()}
    assert all(i["Track ID"] in ids for p in back["Playlists"] for i in p["Playlist Items"]), "playlist references a missing track"
    print(f"wrote {out}\nplaylist '{a.name}' with {len(items)} tracks:")
    for i, f in enumerate(files, 1):
        t = tracks[str(1000 + i)]
        print(f"  {i}. {t.get('Artist','?')} - {t['Name']}  ({t.get('Total Time', 0)/1000:.0f}s)   [{f}]")
    print("\nIn Traktor: right-click the iTunes node > Refresh Playlists (the iTunes/Music path must point at this file).")


if __name__ == "__main__":
    main()