"""Traktor's own waveform data, read from disk (read-only).

Traktor keeps one overview "stripe" per analysed track in `Documents\\Native Instruments\\Traktor <version>\\Stripes\\NNN\\<KEY>`:
a 25-byte header (`06 'PRTS' ...`) followed by 65 536 columns of 4 bytes, stretched over the whole track whatever its
length. Bytes 0-2 of a column are the low, mid and high band levels; byte 3 is NOT audio (it carries a 255 marker every 64 columns
even in silence), so drawing code must ignore it.
A companion `<KEY>.stripe` file is touched whenever the track is loaded, which is how a loaded deck is matched to its
stripe (the key is not stored in the audio file or the collection).
"""
import glob
import os

HEADER = 25
COLUMNS = 65536


def stripes_root(base=None):
    """The newest `Traktor <version>\\Stripes` folder, or None."""
    if base is None:
        base = os.environ.get("RIPPER_STRIPES_DIR")
        if base:
            return base
        docs = os.path.join(os.path.expanduser("~"), "Documents", "Native Instruments")
        found = sorted(glob.glob(os.path.join(docs, "Traktor *", "Stripes")))
        return found[-1] if found else None
    return base


def list_stripes(root):
    """{key: time it was last loaded or written} for every stripe in the folder."""
    out = {}
    if not root or not os.path.isdir(root):
        return out
    for path in glob.glob(os.path.join(root, "*", "*")):
        name = os.path.basename(path)
        if name.endswith(".stripe"):
            continue
        try:
            stamp = os.path.getmtime(path + ".stripe") if os.path.exists(path + ".stripe") else os.path.getmtime(path)
        except OSError:
            continue
        out[name] = (stamp, path)
    return out


def pick_stripe(stripes, loaded_at, taken=(), before=20.0, after=30.0):
    """The stripe touched closest to the moment the track was loaded (Traktor touches it a few seconds before or
    after the mod reports the load), not one already given to another track; None when nothing is close enough."""
    best = None
    for key, (stamp, _path) in stripes.items():
        gap = stamp - loaded_at
        if key in taken or gap < -before or gap > after:
            continue
        if best is None or abs(gap) < best[0]:
            best = (abs(gap), key)
    return best[1] if best else None


def read_columns(path, columns=16384):
    """`columns` x 4 bytes (max of each group of source columns) for drawing, or None when the file is not a stripe."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if len(raw) != HEADER + COLUMNS * 4 or raw[1:5] != b"PRTS":
        return None
    data = raw[HEADER:]
    group = COLUMNS // columns
    if group <= 1:
        return data
    out = bytearray(columns * 4)
    for i in range(columns):
        block = data[i * group * 4:(i + 1) * group * 4]
        for j in range(4):
            out[i * 4 + j] = max(block[j::4])
    return bytes(out)
