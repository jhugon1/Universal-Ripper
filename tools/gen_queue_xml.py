#!/usr/bin/env python3
"""tools/gen_queue_xml.py - write an iTunes-style library XML holding ONE playlist, for Traktor's iTunes node.
  python tools/gen_queue_xml.py                      newest 3 audio files under ~/Music/Traktor -> 'Ripper Queue'
  python tools/gen_queue_xml.py --newest 5
  python tools/gen_queue_xml.py "C:\\path\\a.mp3" "C:\\path\\b.mp3"   explicit files, in this order"""
import argparse, hashlib, os, plistlib, re, sys
from pathlib import Path

AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif"}
HOME = Path(os.environ.get("USERPROFILE") or Path.home())


def pid(s): return hashlib.md5(s.encode()).hexdigest()[:16].upper()
def uri(p): return p.resolve().as_uri().replace("file:///", "file://localhost/", 1)


def meta(p):
    m = {"name": p.stem, "artist": "", "album": "", "genre": "", "ms": None}
    if " - " in p.stem:
        m["artist"], m["name"] = (x.strip() for x in p.stem.split(" - ", 1))
    try:
        import mutagen
        f = mutagen.File(p, easy=True)
        if f is not None:
            for k, key in (("name", "title"), ("artist", "artist"), ("album", "album"), ("genre", "genre")):
                if f.get(key): m[k] = str(f[key][0])
            if f.info and getattr(f.info, "length", None): m["ms"] = int(f.info.length * 1000)
    except ImportError:
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

    tracks, items = {}, []
    for i, f in enumerate(files, 1):
        m = meta(f)
        t = {"Track ID": i, "Name": m["name"], "Artist": m["artist"], "Album": m["album"], "Genre": m["genre"],
             "Kind": "Audio file", "Size": f.stat().st_size, "Track Type": "File", "Location": uri(f), "Persistent ID": pid(str(f))}
        if m["ms"]: t["Total Time"] = m["ms"]
        tracks[str(i)] = {k: v for k, v in t.items() if v not in ("", None)}
        items.append({"Track ID": i})
    lib = {"Major Version": 1, "Minor Version": 1, "Application Version": "12.0", "Features": 5, "Show Content Ratings": True,
           "Music Folder": uri(Path(a.source)) + "/", "Library Persistent ID": pid("ripper-library"), "Tracks": tracks,
           "Playlists": [{"Name": a.name, "Playlist ID": 1000, "Playlist Persistent ID": pid(a.name), "All Items": True, "Playlist Items": items}]}

    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fp:
        plistlib.dump(lib, fp, fmt=plistlib.FMT_XML, sort_keys=False)
    back = plistlib.loads(out.read_bytes())  # round-trip check
    ids = {t["Track ID"] for t in back["Tracks"].values()}
    assert all(i["Track ID"] in ids for i in back["Playlists"][0]["Playlist Items"]), "playlist references a missing track"
    print(f"wrote {out}\nplaylist '{a.name}' with {len(items)} tracks:")
    for i, f in enumerate(files, 1): print(f"  {i}. {tracks[str(i)].get('Artist','?')} - {tracks[str(i)]['Name']}   [{f}]")
    print("\nIn Traktor: Preferences > File Management > iTunes library/XML path -> choose this file"
          "\n(tick 'Use legacy XML iTunes library' if that option is shown), then right-click the iTunes node > Refresh Playlists.")


if __name__ == "__main__":
    main()