"""Download worker for queue items.

`process()` does one item end to end (download into the library if needed, tag, link into tonight's
folder) and is plain synchronous code, run in a thread. `QueueWorker` is the asyncio loop that claims queued
items, runs one or two at a time, retries the order prefixes of files that were busy, and reports
results. The outside world (YouTube lookup, yt-dlp) comes in through a `backend` object.
"""
import asyncio
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import files, nml, rip, store
from .rip import clean_error

MAX_ATTEMPTS = 2        # one retry
CONCURRENCY = 2
IDLE_POLL = 2.0         # seconds between looks at the queue when nothing woke the worker
MARKER_RETRY_EVERY = 15.0


def process(db_path, item_id, root, backend):
    """Finish one claimed item: make sure its video is in the library (downloading it only if it is not), then
    link it into tonight's folder. Returns "ready", "failed" or "removed"."""
    conn = store.open_db(db_path)
    try:
        error = None
        for _ in range(MAX_ATTEMPTS):
            try:
                return _attempt(conn, item_id, root, backend)
            except Exception as exc:  # network, yt-dlp, ffmpeg, disk: all end up as a failed item
                error = exc
        store.update_item(conn, item_id, status="failed", error=clean_error(error))
        return "failed"
    finally:
        conn.close()


def _attempt(conn, item_id, root, backend):
    item = store.get_item(conn, item_id)
    if not item or item["status"] != "downloading":
        return "removed"
    entry = rip.ensure_in_library(conn, backend, video_id=item["video_id"], artist=item["artist"], title=item["title"],
                                  album=item["album"], thumbnail=item["thumbnail"])
    item = store.get_item(conn, item_id)  # the DJ may have removed it while it downloaded
    if item["status"] != "downloading":
        return "removed"  # the rip stays in the library; nothing is put into tonight's folder
    rip.link_item(conn, item, entry, root)
    return "ready"


class QueueWorker:
    def __init__(self, db_path, root, backend, on_result, concurrency=CONCURRENCY):
        """db_path and root are callables (read on every use, so tests can change the environment)."""
        self.db_path, self.root, self.backend, self.on_result = db_path, root, backend, on_result
        self.concurrency = concurrency
        self._task = None
        self._loop = None
        self._wake = None

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def wake_threadsafe(self):
        """Tell the worker there is something new (callable from any thread)."""
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def stop(self):
        if self._task:
            self._task.cancel()

    def _claim(self):
        conn = store.open_db(self.db_path())
        try:
            return store.claim_next(conn)
        finally:
            conn.close()

    def _startup(self):
        conn = store.open_db(self.db_path())
        try:
            store.reset_stale_downloads(conn)  # whatever was downloading when we last stopped
        finally:
            conn.close()

    def _periodic(self):
        """Retry file names that were busy, and pick up analyzed BPMs from Traktor's collection (read-only)."""
        conn = store.open_db(self.db_path())
        try:
            files.sync_markers(conn, self.root())
            collection = nml.collection_path()
            if collection:
                nml.fill_bpm(conn, nml.bpm_index(collection))
        finally:
            conn.close()

    async def _work(self, item_id):
        outcome = await run_in_threadpool(process, self.db_path(), item_id, self.root(), self.backend)
        await self.on_result(item_id, outcome)

    async def _run(self):
        await run_in_threadpool(self._startup)
        running, last_markers = set(), 0.0
        while True:
            running = {t for t in running if not t.done()}
            while len(running) < self.concurrency:
                item = await run_in_threadpool(self._claim)
                if not item:
                    break
                running.add(asyncio.create_task(self._work(item["id"])))
            if time.monotonic() - last_markers >= MARKER_RETRY_EVERY:
                last_markers = time.monotonic()
                try:
                    await run_in_threadpool(self._periodic)
                except Exception as exc:
                    print(f"Queue: periodic maintenance failed: {clean_error(exc)}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
