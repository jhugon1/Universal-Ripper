"""HTTP receiver for the Traktor mod's POSTs, served on 127.0.0.1 only.

The mod posts to http://localhost:8080/<kind>[/<arg>]. This is a separate ASGI app on its own
port, so the guest-facing app on :8000 (exposed through ngrok) never serves these routes.
"""
import asyncio
import json
import logging
import os
import socket

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("traktor.receiver")

DEFAULT_PORT = 8080


def configured_port(environ=None) -> int:
    """RIPPER_TRAKTOR_PORT: default 8080, 0 turns the receiver off."""
    raw = (environ if environ is not None else os.environ).get("RIPPER_TRAKTOR_PORT")
    if raw in (None, ""):
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        print(f"Traktor receiver: ignoring invalid RIPPER_TRAKTOR_PORT={raw!r}, using {DEFAULT_PORT}")
        return DEFAULT_PORT
    return port if 0 <= port <= 65535 else DEFAULT_PORT


FAKE_HEADER = "x-fake-traktor"  # tools/fake_traktor.py marks every event it sends with this


def create_receiver(state, on_change=None, info=None, allow_fake=None) -> FastAPI:
    """ASGI app: POST /{kind} and /{kind}/{arg}. Always answers {} quickly, whatever the body."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def about():
        """Lets tools such as fake_traktor check what they are about to feed before posting."""
        return JSONResponse({"app": "ripper-traktor-receiver", **(info() if info else {})})

    @app.post("/{kind}")
    @app.post("/{kind}/{arg}")
    async def receive(request: Request, kind: str, arg: str = ""):
        if request.headers.get(FAKE_HEADER) and allow_fake is not None and not allow_fake():
            # a test tool is feeding the REAL database (for example a loop left running): refuse, never record it
            return JSONResponse({"error": "fake events are refused: the app is using the real database"}, status_code=403)
        try:
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = None  # not JSON: ignore it, the mod must still get a 200
            if body is not None:
                changes = state.apply(request.url.path, body)
                if changes and on_change:
                    on_change(changes)
        except Exception:  # never let a bad event break the mod's request
            log.exception("error handling %s", request.url.path)
        return JSONResponse({})

    return app


def _bind(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":  # on Windows SO_REUSEADDR would let a second process share the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class TraktorReceiver:
    """Runs the receiver as a second uvicorn server inside the running event loop."""

    def __init__(self, state, port, on_change=None, host="127.0.0.1", retry_seconds=3.0, info=None, allow_fake=None):
        self.state, self.port, self.on_change, self.info, self.allow_fake = state, port, on_change, info, allow_fake
        self.host, self.retry_seconds = host, retry_seconds
        self.bound = False
        self._server = None
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def _run(self):
        # Bind ourselves: uvicorn calls sys.exit() when it cannot bind, which must not take the app down.
        # After a dev reload the old process may still hold the port for a moment, so retry briefly.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.retry_seconds
        sock = None
        while sock is None:
            try:
                sock = _bind(self.host, self.port)
            except OSError as exc:
                if loop.time() >= deadline:
                    print(f"Traktor receiver: {self.host}:{self.port} unavailable ({exc.strerror or exc}); "
                          "running without Traktor input")
                    return
                await asyncio.sleep(0.2)
        config = uvicorn.Config(create_receiver(self.state, self.on_change, self.info, self.allow_fake), log_config=None,
                                log_level="warning", access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # the main server owns signals
        self.bound = True
        print(f"Traktor receiver listening on http://{self.host}:{self.port}")
        try:
            await self._server.serve(sockets=[sock])
        finally:
            self.bound = False
            sock.close()

    async def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None and self._server is None:
            self._task.cancel()  # still retrying to bind: nothing to shut down gracefully
        elif self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
