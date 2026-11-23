"""Make a Windows .ico from the Ripper face (used for the Desktop shortcut).  python tools/make_icon.py OUT.ico"""
import sys
from pathlib import Path

from PIL import Image

SOURCE = Path(__file__).resolve().parent.parent / "static" / "ripped_face.jpg"


def main(out):
    img = Image.open(SOURCE).convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
