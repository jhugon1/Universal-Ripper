from filenames import track_basename, ytdlp_outtmpl


def test_basename():
    assert track_basename("Daft Punk", "One More Time") == "Daft Punk - One More Time"
    assert track_basename('AC/DC', 'Who Made Who?') == "ACDC - Who Made Who"
    assert track_basename("", "Solo") == "Solo"
    assert track_basename("", "") == "Unknown"


def test_outtmpl_escapes_percent():
    assert ytdlp_outtmpl("d", "A", "100%") == "d/A - 100%%.%(ext)s"
