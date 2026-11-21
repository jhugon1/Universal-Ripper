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
