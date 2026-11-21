"""Dev-only browser live reload. Active only when RIPPER_DEV=1 (set by `run.py --dev`).

- `/api/dev/reload` is an SSE stream carrying the server's boot id and a file-change counter.
- A small middleware injects a script into HTML pages that reloads the tab when either changes.
  A server restart changes the boot id; edits to templates/ and static/ bump the counter, so
  html/css/js edits reload the page without restarting the server.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

ROOT = Path(__file__).resolve().parent
WATCH_DIRS = [ROOT / "templates", ROOT / "static"]
WATCH_EXTS = {".html", ".css", ".js"}
POLL_SECONDS = 0.3

BOOT_ID = uuid.uuid4().hex
_version = 0

SNIPPET = (
    b"<script>(function(){var seen=null;var es=new EventSource('/api/dev/reload');"
    b"es.onmessage=function(e){var d=JSON.parse(e.data);var k=d.boot+':'+d.version;"
    b"if(seen===null){seen=k;}else if(k!==seen){location.reload();}};})();</script>"
)


def is_dev() -> bool:
    return os.environ.get("RIPPER_DEV") == "1"


def scan(dirs=None) -> dict:
    """mtime of every watched file under the given directories."""
    found = {}
    for d in dirs if dirs is not None else WATCH_DIRS:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.suffix.lower() in WATCH_EXTS and p.is_file():
                try:
                    found[str(p)] = p.stat().st_mtime_ns
                except OSError:
                    pass
    return found


async def watch_files(dirs=None, poll=POLL_SECONDS):
    """Bump the change counter whenever a watched file is added, removed or modified."""
    global _version
    last = scan(dirs)
    while True:
        await asyncio.sleep(poll)
        current = scan(dirs)
        if current != last:
            last = current
            _version += 1


async def reload_events(is_disconnected, poll=0.25, keepalive=15.0):
    """SSE body: reconnect delay, current boot/version, then a message on every change."""
    yield "retry: 500\n\n"
    sent = None
    idle = 0.0
    while not await is_disconnected():
        if _version != sent:
            sent = _version
            yield f"data: {json.dumps({'boot': BOOT_ID, 'version': sent})}\n\n"
            idle = 0.0
        elif idle >= keepalive:
            yield ": keepalive\n\n"
            idle = 0.0
        await asyncio.sleep(poll)
        idle += poll


class InjectLiveReload:
    """Pure ASGI middleware: add SNIPPET to text/html responses, leave everything else alone."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = None
        chunks = []

        async def wrapped(message):
            nonlocal start
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers") or []).get(b"content-type", b"")
                if ctype.startswith(b"text/html"):
                    start = message
                    return
            elif message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                if b"</body>" in body:
                    body = body.replace(b"</body>", SNIPPET + b"</body>", 1)
                else:
                    body += SNIPPET
                headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, wrapped)


def install(app: FastAPI):
    """Add the reload endpoint and the page-injection middleware."""

    @app.get("/api/dev/reload")
    async def dev_reload(request: Request):
        return StreamingResponse(reload_events(request.is_disconnected), media_type="text/event-stream")

    app.add_middleware(InjectLiveReload)
