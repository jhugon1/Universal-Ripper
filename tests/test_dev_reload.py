import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

import devreload
import run


def _page_app():
    app = FastAPI()

    @app.get("/page", response_class=HTMLResponse)
    def page():
        return "<html><body><h1>hi</h1></body></html>"

    @app.get("/nobody", response_class=HTMLResponse)
    def nobody():
        return "<p>fragment</p>"

    @app.get("/data")
    def data():
        return JSONResponse({"a": 1})

    devreload.install(app)
    return app


def test_snippet_injected_into_html_only():
    c = TestClient(_page_app())
    r = c.get("/page")
    assert "/api/dev/reload" in r.text
    assert r.text.index("/api/dev/reload") < r.text.index("</body>")
    assert int(r.headers["content-length"]) == len(r.content)
    assert "/api/dev/reload" in c.get("/nobody").text  # no </body>: appended
    assert c.get("/data").json() == {"a": 1}
    assert "script" not in c.get("/data").text


def test_not_installed_without_dev(monkeypatch):
    monkeypatch.delenv("RIPPER_DEV", raising=False)
    assert devreload.is_dev() is False
    import app as app_module

    assert not any(getattr(r, "path", "") == "/api/dev/reload" for r in app_module.app.routes)
    monkeypatch.setenv("RIPPER_DEV", "1")
    assert devreload.is_dev() is True


def test_scan_sees_changes(tmp_path):
    f = tmp_path / "a.html"
    f.write_text("one")
    (tmp_path / "ignored.txt").write_text("x")
    before = devreload.scan([tmp_path])
    assert list(before) == [str(f)]
    f.write_text("two")
    os.utime(f, ns=(1, 10**18))  # make the mtime differ regardless of filesystem resolution
    assert devreload.scan([tmp_path]) != before


def test_events_report_boot_and_changes(monkeypatch):
    async def run_stream():
        disconnected = False

        async def is_disconnected():
            return disconnected

        gen = devreload.reload_events(is_disconnected, poll=0.01)
        assert await gen.__anext__() == "retry: 500\n\n"
        first = json.loads((await gen.__anext__()).removeprefix("data: "))
        assert first["boot"] == devreload.BOOT_ID
        monkeypatch.setattr(devreload, "_version", first["version"] + 1)
        second = json.loads((await gen.__anext__()).removeprefix("data: "))
        assert second["version"] == first["version"] + 1
        disconnected = True
        assert [m async for m in gen] == []

    asyncio.run(run_stream())


def test_uvicorn_kwargs():
    assert "reload" not in run.uvicorn_kwargs(False)
    dev = run.uvicorn_kwargs(True)
    assert dev["reload"] is True and "*.py" in dev["reload_includes"]
    assert Path(dev["reload_dirs"][0]) == Path(run.ROOT)


def test_dev_mode_never_defaults_to_the_real_database(tmp_path):
    env = {}
    chosen = run.dev_defaults(env, root=tmp_path)
    assert set(chosen) == {"RIPPER_DB", "RIPPER_QUEUE_DIR"}
    assert Path(env["RIPPER_DB"]) == tmp_path / "dev_data" / "queue.dev.db" and Path(env["RIPPER_DB"]).name != "queue.db"
    assert Path(env["RIPPER_QUEUE_DIR"]) == tmp_path / "dev_data" / "Ripper Queue"
    assert (tmp_path / "dev_data").is_dir()


def test_dev_defaults_respect_what_the_caller_set(tmp_path):
    env = {"RIPPER_DB": "mine.db"}
    assert set(run.dev_defaults(env, root=tmp_path)) == {"RIPPER_QUEUE_DIR"}
    assert env["RIPPER_DB"] == "mine.db"
    full = {"RIPPER_DB": "a.db", "RIPPER_QUEUE_DIR": "q"}
    assert run.dev_defaults(full, root=tmp_path / "other") == {} and not (tmp_path / "other" / "dev_data").exists()


def test_window_mode_opens_the_control_page_and_the_booth():
    assert run.window_urls(False) == [run.DJ_URL]
    assert run.window_urls(True) == [run.DJ_URL, run.BOOTH_URL]
    assert run.window_urls(True, no_booth=True) == [run.DJ_URL]
    assert run.window_urls(False, no_booth=True) == [run.DJ_URL]


def test_startup_names_the_branch_and_warns_when_the_queue_code_is_missing(tmp_path):
    lines = run.checkout_report(tmp_path)  # not a git checkout, and no queue code
    assert lines[0].startswith("Branch: unknown") and "WARNING" in lines[1] and "ripper_queue" in lines[1]
    package = tmp_path / "ripper_queue"
    package.mkdir()
    for name in ("store.py", "worker.py", "files.py"):
        (package / name).write_text("")
    assert run.queue_code_present(tmp_path) and len(run.checkout_report(tmp_path)) == 1
    (package / "worker.py").unlink()
    assert not run.queue_code_present(tmp_path)  # a half-present package counts as missing
    assert run.current_branch(Path(run.ROOT)) not in (None, "")  # this checkout is a git repo
