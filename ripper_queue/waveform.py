"""Our own waveform, computed from the audio file (no dependency beyond ffmpeg, which the app already needs).

ffmpeg splits the track into three bands (low / mid / high) and we take the peak of each band for every 2 ms of audio.
The result is one byte per band per column (0-255, on the scale Traktor's own analysis uses, so the page can colour it
the way Traktor does). It is computed once per file and cached; computing never blocks the app (see WaveformCache).

Cache file layout: 8-byte header `RWV1` + uint32 columns (little endian), then columns x 3 bytes (low, mid, high).
"""
import hashlib
import os
import struct
import subprocess
import tempfile
import threading
from array import array
from pathlib import Path

RATE = 22050                 # decode rate: bands up to ~11 kHz
HOP = 44                     # samples per column: 2 ms
LOW_HZ, HIGH_HZ = 800, 5000  # band edges, chosen so a real track colours like Traktor's own display
MAGIC = b"RWV1"
GAIN = (0.55, 0.6, 1.1)       # peak of a band -> 0..255, tuned by eye against Traktor's display (loud music tops out near 140)


def cache_dir():
    base = os.environ.get("RIPPER_WAVEFORM_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache"), "RippedRipper", "waveforms")
    return Path(base)


def cache_path(audio_path):
    st = os.stat(audio_path)
    key = hashlib.sha1(f"{os.path.abspath(audio_path)}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:24]
    return cache_dir() / f"{key}.wave"


def ffmpeg_command(path):
    graph = (f"[0:a]aformat=sample_rates={RATE}:channel_layouts=mono,asplit=3[a][b][c];"
             f"[a]lowpass=f={LOW_HZ}[l];[b]highpass=f={LOW_HZ},lowpass=f={HIGH_HZ}[m];[c]highpass=f={HIGH_HZ}[h];"
             "[l][m][h]amerge=inputs=3")
    return ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-filter_complex", graph, "-f", "s16le", "-ac", "3", "-"]


def columns_from_pcm(raw, hop=HOP, gain=GAIN):
    """Interleaved 3-channel s16le bytes -> bytes (columns x 3): the peak of every band in every `hop` samples."""
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 6 * 6])
    bands = [samples[i::3] for i in range(3)]
    n = len(bands[0]) // hop
    out = bytearray(n * 3)
    for b in range(3):
        scale = 255.0 * gain[b] / 32768.0
        data = bands[b]
        for i in range(n):
            chunk = data[i * hop:(i + 1) * hop]
            peak = max(max(chunk), -min(chunk))
            v = int(peak * scale)
            out[i * 3 + b] = 255 if v > 255 else v
    return bytes(out)


def compute(path):
    """The waveform columns of an audio file (bytes, columns x 3). Raises RuntimeError when ffmpeg cannot read it."""
    proc = subprocess.run(ffmpeg_command(path), capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg could not read the audio")
    return columns_from_pcm(proc.stdout)


def pack(columns: bytes) -> bytes:
    return MAGIC + struct.pack("<I", len(columns) // 3) + columns


def unpack(blob: bytes):
    """-> (columns, bytes) or None when the blob is not a waveform file."""
    if len(blob) < 8 or blob[:4] != MAGIC:
        return None
    n = struct.unpack("<I", blob[4:8])[0]
    return (n, blob[8:]) if len(blob) == 8 + n * 3 else None


class WaveformCache:
    """get(path) answers at once: the cached waveform, or None while it is being computed in the background."""

    def __init__(self, compute_fn=compute):
        self._compute = compute_fn
        self._busy = set()
        self._failed = {}
        self._lock = threading.Lock()

    def get(self, audio_path):
        try:
            target = cache_path(audio_path)
        except OSError:
            return None
        if target.is_file():
            got = unpack(target.read_bytes())
            if got:
                return got
        with self._lock:
            if target in self._busy or target in self._failed:
                return None
            self._busy.add(target)
        threading.Thread(target=self._work, args=(audio_path, target), daemon=True).start()
        return None

    def failed(self, audio_path):
        try:
            return cache_path(audio_path) in self._failed
        except OSError:
            return False

    def precompute(self, audio_path):
        self.get(audio_path)

    def _work(self, audio_path, target):
        try:
            blob = pack(self._compute(audio_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".part")
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, target)
        except Exception as exc:  # unreadable file: remember, do not retry forever
            with self._lock:
                self._failed[target] = str(exc)[:120]
        finally:
            with self._lock:
                self._busy.discard(target)


CACHE = WaveformCache()  # shared by the app (deck display) and the ripper (computed as soon as a rip is saved)
