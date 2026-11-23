#!/usr/bin/env python3
"""tools/make_assets.py - regenerate the derived artwork from static/Ripped.jpg with ffmpeg (no other dependencies).

  python tools/make_assets.py

  static/ripped_wordmark.png   the RIPPED lettering (crop 935x268 at x=40, y=180)
  static/ripped_face.jpg       the face, square (crop 900x900 at x=61, y=520): toast fallback art, favicon, splash
The results are committed, so this only needs to run when the source artwork changes."""
import subprocess
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
JOBS = (
    ("ripped_wordmark.png", ["-vf", "crop=935:268:40:180"]),
    ("ripped_face.jpg", ["-vf", "crop=900:900:61:520", "-q:v", "3"]),
)


def main():
    source = STATIC / "Ripped.jpg"
    if not source.is_file():
        sys.exit(f"missing {source}")
    for name, args in JOBS:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), *args, str(STATIC / name)], check=True)
        print("wrote", STATIC / name)


if __name__ == "__main__":
    main()
