import time

import pytest

from ripper_queue import store


@pytest.fixture
def db(tmp_path):
    conn = store.open_db(tmp_path / "q.db")
    store.ensure_schema(conn)
    yield conn
    conn.close()


def day(y, m, d, h=12):
    return time.mktime((y, m, d, h, 0, 0, 0, 0, -1))


def add(db, n, **kw):
    return store.add_item(db, source="dj", artist=f"Artist {n}", title=f"Title {n}", **kw)[0]


# ---- sessions --------------------------------------------------------------------------------
def test_first_use_creates_a_dated_session(db):
    s = store.current_session(db, now=day(2026, 11, 21))
    assert s["name"] == "2026-11-21" and s["ended_at"] is None
    assert store.current_session(db, now=day(2026, 11, 22))["id"] == s["id"]  # a set past midnight stays in its session


def test_new_night_ends_the_session_and_names_it_by_date(db):
    first = store.current_session(db, now=day(2026, 11, 21))
    add(db, 1, now=day(2026, 11, 21))
    second = store.new_night(db, now=day(2026, 11, 22))
    assert second["name"] == "2026-11-22" and second["id"] != first["id"]
    assert db.execute("SELECT ended_at FROM sessions WHERE id = ?", (first["id"],)).fetchone()[0] is not None
    assert store.current_session(db)["id"] == second["id"]


def test_second_session_on_the_same_day_gets_a_suffix(db):
    store.current_session(db, now=day(2026, 11, 21, 18))
    add(db, 1, now=day(2026, 11, 21, 18))
    assert store.new_night(db, now=day(2026, 11, 21, 23))["name"] == "2026-11-21-2"
    add(db, 2, now=day(2026, 11, 21, 23))
    assert store.new_night(db, now=day(2026, 11, 21, 23))["name"] == "2026-11-21-3"


def test_new_night_on_an_empty_session_does_nothing(db):
    s = store.current_session(db, now=day(2026, 11, 21))
    assert store.new_night(db, now=day(2026, 11, 21))["id"] == s["id"]
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_items_belong_to_the_current_session_only(db):
    a = add(db, 1)
    store.new_night(db)
    b = add(db, 2)
    assert a["session_id"] != b["session_id"] and b["position"] == 1
    assert [i["id"] for i in store.list_items(db, b["session_id"])] == [b["id"]]


# ---- items and positions ---------------------------------------------------------------------
def test_positions_increase_and_never_repeat(db):
    items = [add(db, n) for n in (1, 2, 3)]
    assert [i["position"] for i in items] == [1, 2, 3]
    store.update_item(db, items[2]["id"], status="removed")
    assert add(db, 4)["position"] == 4  # a removed item's number is not reused


def test_a_request_gets_one_live_item(db):
    first, created = store.add_item(db, source="request", request_id=7, artist="A", title="T")
    again, created2 = store.add_item(db, source="request", request_id=7, artist="A", title="T")
    assert created and not created2 and first["id"] == again["id"]
    store.update_item(db, first["id"], status="failed")
    assert store.add_item(db, source="request", request_id=7, artist="A", title="T")[1] is True  # failed: may be re-approved


def test_claim_takes_the_lowest_position_once(db):
    a, b = add(db, 1), add(db, 2)
    store.move_item(db, b["id"], "up")
    assert store.claim_next(db)["id"] == b["id"]
    assert store.claim_next(db)["id"] == a["id"]
    assert store.claim_next(db) is None
    store.reset_stale_downloads(db)
    assert store.get_item(db, a["id"])["status"] == "queued"


def item(i, position, status="ready"):
    return {"id": i, "position": position, "status": status}


def test_plan_move_swaps_with_the_neighbour():
    items = [item(1, 1), item(2, 2), item(3, 3)]
    assert store.plan_move(items, 2, "up") == {2: 1, 1: 2}
    assert store.plan_move(items, 2, "down") == {2: 3, 3: 2}
    assert store.plan_move(items, 1, "up") == {} and store.plan_move(items, 3, "down") == {}
    assert store.plan_move(items, 99, "up") == {} and store.plan_move(items, 2, "sideways") == {}


def test_plan_move_never_touches_locked_items():
    items = [item(1, 1, "played"), item(2, 2), item(3, 3, "loaded"), item(4, 4), item(5, 5, "removed"), item(6, 6, "failed")]
    assert store.plan_move(items, 2, "up") == {}            # the played item above is not a neighbour
    assert store.plan_move(items, 2, "down") == {2: 4, 4: 2}  # skips the loaded item, which keeps position 3
    assert store.plan_move(items, 3, "up") == {} and store.plan_move(items, 1, "down") == {}
    assert store.plan_move(items, 6, "up") == {}


def test_move_item_updates_positions_in_the_database(db):
    a, b, c = add(db, 1), add(db, 2), add(db, 3)
    changed = store.move_item(db, c["id"], "up")
    assert sorted(changed) == sorted([b["id"], c["id"]])
    assert [i["id"] for i in store.list_items(db, a["session_id"])] == [a["id"], c["id"], b["id"]]


# ---- the re-select hint ----------------------------------------------------------------------
def test_folder_hint_until_acknowledged(db):
    assert store.folder_state(db)["reselect_needed"] is False
    store.mark_folder_changed(db, now=100.0)
    assert store.folder_state(db)["reselect_needed"] is True
    store.ack_folder(db, now=101.0)
    assert store.folder_state(db)["reselect_needed"] is False
    store.mark_folder_changed(db, now=102.0)
    assert store.folder_state(db)["reselect_needed"] is True
