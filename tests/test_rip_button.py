import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the RIP button is Windows only")


def test_button_follows_the_traktor_window():
    import rip_button_window as rb
    x, y = rb.button_position((100, 50, 1900, 1000))
    assert x == 100 + rb.OFFSET_FROM_LEFT and y == 50 + rb.OFFSET_FROM_TOP
    assert rb.button_position((0, 0, 1900, 1000))[0] < rb.button_position((100, 0, 1900, 1000))[0]   # moves with the window


def test_button_only_shows_for_traktor_or_the_ripper():
    import rip_button_window as rb
    assert rb.visible_for("Traktor Pro 4") and rb.visible_for("Ripped Ripper")
    assert not rb.visible_for("Google Chrome") and not rb.visible_for("")
    assert rb.visible_for("tk")   # our own panel in front counts too
