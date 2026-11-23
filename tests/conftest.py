import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

# Process-wide defaults, set before anything imports the app: a test that forgets to set up its own folders
# (or undoes its setup) can never reach the user's real database, library, queue folder or Traktor folder.
_SANDBOX = Path(tempfile.mkdtemp(prefix="ripper_tests_"))
for _name, _value in (("RIPPER_DB", _SANDBOX / "default.db"), ("RIPPER_QUEUE_DIR", _SANDBOX / "Ripper Queue"),
                      ("RIPPER_LIBRARY_DIR", _SANDBOX / "Library"), ("RIPPER_PLAYLIST_DIR", _SANDBOX / "Playlist Rips"),
                      ("RIPPER_TRAKTOR_DIR", _SANDBOX / "no-traktor-here"), ("RIPPER_OLD_RIPS_DIR", _SANDBOX / "no-old-rips"),
                      ("RIPPER_TRAKTOR_PORT", "0"), ("RIPPER_POPUP", "0"), ("RIPPER_WAVEFORM_DIR", _SANDBOX / "waveforms")):
    os.environ[_name] = str(_value)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_from_the_real_machine(monkeypatch, tmp_path):
    """Per test: its own temp folders, and no listener on :8080."""
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", "0")
    monkeypatch.setenv("RIPPER_TRAKTOR_DIR", str(tmp_path / "no-traktor-here"))
    monkeypatch.setenv("RIPPER_LIBRARY_DIR", str(tmp_path / "Library"))
    monkeypatch.setenv("RIPPER_PLAYLIST_DIR", str(tmp_path / "Playlist Rips"))
    monkeypatch.setenv("RIPPER_OLD_RIPS_DIR", str(tmp_path / "no-old-rips-here"))


@pytest.fixture(scope="session")
def tone_mp3(tmp_path_factory):
    """A real 1 s mp3 to copy and tag (needs ffmpeg)."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("tone") / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-b:a", "128k", str(path)], check=True)
    return path
