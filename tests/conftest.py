import pytest


@pytest.fixture(autouse=True)
def _no_traktor_listener_by_default(monkeypatch):
    """Keep the test suite off :8080; tests that need the receiver set their own port."""
    monkeypatch.setenv("RIPPER_TRAKTOR_PORT", "0")
