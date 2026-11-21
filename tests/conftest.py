import pytest


@pytest.fixture(autouse=True)
def _isolated_from_the_real_machine(monkeypatch, tmp_path):
    """Keep tests off :8080 and away from the user's real Traktor folder."""
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", "0")
    monkeypatch.setenv("RIPPER_TRAKTOR_DIR", str(tmp_path / "no-traktor-here"))
