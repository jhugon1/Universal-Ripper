import rip_panel as rp


def test_format_length_and_subtitle():
    assert rp.format_length(225) == "3:45" and rp.format_length("61") == "1:01"
    assert rp.format_length(None) == "" and rp.format_length(0) == "" and rp.format_length("x") == ""
    assert rp.subtitle({"artist": "Daft Punk", "album": None, "length_s": 321}) == "Daft Punk · 5:21"
    assert rp.subtitle({}) == ""


def test_item_status_looks_in_every_list():
    payload = {"items": [{"id": 1, "status": "downloading"}], "to_play": [{"id": 2, "status": "ready"}], "played": [{"id": 3, "status": "played", "error": None}]}
    assert rp.item_status(payload, 1) == ("downloading", None)
    assert rp.item_status(payload, 2)[0] == "ready" and rp.item_status(payload, 3)[0] == "played"
    assert rp.item_status(payload, 9) == (None, None) and rp.item_status(None, 1) == (None, None)


def test_rip_outcome_labels():
    assert rp.rip_outcome("new")[2] is False                     # still downloading
    assert rp.rip_outcome("done")[0].startswith("Ripped") and rp.rip_outcome("done")[2] is True
    assert rp.rip_outcome("in_queue")[2] and rp.rip_outcome("duplicate")[2]
    label, colour, finished = rp.rip_outcome("error", "x" * 80)
    assert finished and colour == rp.BAD and len(label) <= 28    # long errors are shortened for the button


def test_api_reports_an_unreachable_app_plainly():
    import pytest
    with pytest.raises(rp.ApiError, match="not reachable"):
        rp.Api("http://127.0.0.1:1").get("/api/queue_items", timeout=1)
