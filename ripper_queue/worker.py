"""Download worker for queue items.

`process()` does one item end to end (look up, download, tag, place in the session folder) and is
plain synchronous code, run in a thread. `QueueWorker` is the asyncio loop that claims queued
items, runs one or two at a time, retries the order prefixes of files that were busy, and reports
results. The outside world (YouTube lookup, yt-dlp) comes in through a `backend` object.
"""
import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import files, nml, store

MAX_ATTEMPTS = 2        # one retry
CONCURRENCY = 2
IDLE_POLL = 2.0         # seconds between looks at the queue when nothing woke the worker
MARKER_RETRY_EVERY = 15.0


def clean_error(exc) -> str:
    """Short message for the UI, without the user's home folder."""
    text = f"{type(exc).__name__}: {exc}".replace(str(Path.home()), "~")
    return text[:300]


def process(db_path, item_id, root, backend):
    """Download and place one claimed item. Returns "ready", "failed" or "removed"."""
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
    meta = backend.lookup(item)
    tmp = Path(tempfile.mkdtemp(prefix="ripper_queue_"))
    try:
        audio = Path(backend.download(meta["video_id"], tmp))
        cover = files.fetch_cover(meta["thumbnail"]) if meta.get("thumbnail") else None
        files.tag_file(audio, title=item["title"], artist=item["artist"], album=meta.get("album"), cover=cover)
        length = files.read_length(audio)

        item = store.get_item(conn, item_id)  # the DJ may have removed it or moved it meanwhile
        if item["status"] != "downloading":
            return "removed"
        session = conn.execute("SELECT name FROM sessions WHERE id = ?", (item["session_id"],)).fetchone()["name"]
        folder = Path(root) / session
        folder.mkdir(parents=True, exist_ok=True)
        target = files.free_path(folder / files.item_filename(item["position"], item["artist"], item["title"]))
        part = target.with_name(target.name + ".part")  # Traktor ignores it until the rename below
        shutil.copyfile(audio, part)
        os.replace(part, target)
        conn.execute("UPDATE queue_items SET status = 'ready', file_path = ?, length_s = ?, marker_position = ?, "
                     "error = NULL WHERE id = ? AND status = 'downloading'",
                     (str(target), length, item["position"], item_id))
        conn.commit()
        store.mark_folder_changed(conn)
        return "ready"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # our own temp folder, never the queue folder


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
