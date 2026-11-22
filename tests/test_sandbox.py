import os
from pathlib import Path


def test_the_suite_cannot_reach_the_real_data():
    """The safety net in conftest: defaults are throw-away places, never the user's real files."""
    import app

    assert app.db_is_real() is False and Path(os.environ["RIPPER_DB"]).name != "queue.db"
    music = str(Path.home() / "Music")
    for name in ("RIPPER_DB", "RIPPER_QUEUE_DIR", "RIPPER_LIBRARY_DIR", "RIPPER_PLAYLIST_DIR", "RIPPER_OLD_RIPS_DIR"):
        assert music not in os.environ[name], name
    assert str(Path.home() / "Documents") not in os.environ["RIPPER_TRAKTOR_DIR"]
