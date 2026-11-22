import shutil
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _isolated_from_the_real_machine(monkeypatch, tmp_path):
    """Keep tests off :8080 and away from the user's real Traktor folder, library and old rips."""
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", "0")
    monkeypatch.setenv("RIPPER_TRAKTOR_DIR", str(tmp_path / "no-traktor-here"))
    monkeypatch.setenv("RIPPER_LIBRARY_DIR", str(tmp_path / "Library"))
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
