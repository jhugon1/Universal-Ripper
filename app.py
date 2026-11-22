import sqlite3
import os
import threading
import time
from fastapi import FastAPI, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from ytmusicapi import YTMusic
import yt_dlp
import devreload
from ripper_queue import files as queue_files
from ripper_queue import library
from ripper_queue import rip as queue_rip
from ripper_queue import search as queue_search
from ripper_queue import store as queue_store
from ripper_queue import sync as queue_sync
from ripper_queue.worker import QueueWorker
from traktor.receiver import TraktorReceiver, configured_port
from traktor.state import TraktorState, file_name_of
import asyncio
import json
from fastapi.responses import StreamingResponse

import subprocess
import sys
from pathlib import Path
from contextlib import asynccontextmanager

def make_console_safe():
    """Windows consoles often use cp1252: printing a track title with other characters (Japanese, emoji)
    would raise UnicodeEncodeError and break a download or an event. Replace what cannot be shown."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # not a real text stream (for example under a test runner)
            pass


make_console_safe()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    init_db()
    await run_in_threadpool(prepare_library)
    restore_traktor_state()
    print("Event loop captured for SSE")
    queue_worker.start()
    watcher = asyncio.create_task(devreload.watch_files()) if devreload.is_dev() else None
    dirty = asyncio.Event()
    pusher = asyncio.create_task(push_traktor_state(dirty))
    receiver = None
    port = configured_port()
    if port:
        # the receiver runs on this event loop, so on_change is called from the loop itself
        receiver = TraktorReceiver(traktor_state, port, on_change=lambda changes: dirty.set(), info=receiver_info)
        receiver.start()
    else:
        print("Traktor receiver off (RIPPER_TRAKTOR_PORT=0)")
    yield
    if watcher:
        watcher.cancel()
    pusher.cancel()
    await queue_worker.stop()
    if receiver:
        await receiver.stop()
    save_traktor_state()
    subscribers.clear()

app = FastAPI(lifespan=lifespan)
if devreload.is_dev():
    devreload.install(app)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
templates = Jinja2Templates(directory="templates")


def db_file() -> str:
    """DB path, read on each use so tests can point RIPPER_DB at a temp file."""
    return os.environ.get("RIPPER_DB", "queue.db")


_thread_local = threading.local()


def get_ytmusic() -> YTMusic:
    """One YTMusic client per thread (it wraps a requests.Session, which is
    not documented as thread-safe)."""
    yt = getattr(_thread_local, "yt", None)
    if yt is None:
        yt = _thread_local.yt = YTMusic()
    return yt



# SSE Notification System
subscribers = set()
loop = None


traktor_state = TraktorState()
TRAKTOR_PUSH_INTERVAL = 0.25  # at most 4 traktor_state events per second


def traktor_event():
    return {"event": "traktor_state", "state": traktor_state.snapshot()}


def prepare_library():
    """Start-up: create the library folder, copy the old standalone rips in on the first run, index the folder."""
    conn = queue_store.open_db(db_file())
    try:
        old = os.environ.get("RIPPER_OLD_RIPS_DIR") or str(Path(__file__).resolve().parent / "DJ_Search_Rips")
        counts = library.ensure_ready(conn, old_dir=old, migrate=not devreload.is_dev())
        print(f"Library: {counts}")
    except Exception as exc:  # a broken library folder must not stop the app
        print(f"Library could not be prepared: {exc}")
    finally:
        conn.close()


def db_is_real() -> bool:
    """True when the app uses the default queue.db, the user's real data."""
    return Path(db_file()).resolve() == Path("queue.db").resolve()


def receiver_info():
    return {"database": {"real": db_is_real(), "name": Path(db_file()).name}}


def sync_queue_with_traktor(decks, now):
    """Played / BPM updates for queue items, from what the decks have loaded."""
    conn = queue_store.open_db(db_file())
    try:
        return queue_sync.apply_sync(conn, decks, now)
    except Exception as exc:  # never let the queue break the live feed
        print(f"Queue sync failed: {exc}")
        return []
    finally:
        conn.close()


TRAKTOR_SAVE_INTERVAL = 2.0   # persist the state at most this often
TRAKTOR_RESTORE_MAX_AGE = 12 * 3600  # a snapshot older than this is not restored after a restart


def save_traktor_state():
    """Write the state (including file paths) to the local DB so a restart can recover it."""
    try:
        data = json.dumps(traktor_state.dump())
        conn = sqlite3.connect(db_file(), timeout=10)
        conn.execute("INSERT OR REPLACE INTO traktor_state (id, saved_at, data) VALUES (1, ?, ?)",
                     (time.time(), data))
        conn.commit()
        conn.close()
    except Exception as exc:  # persistence must never break the live feed
        print(f"Could not save Traktor state: {exc}")


def restore_traktor_state():
    """Reload the last saved state, marked restored, unless it is too old or unreadable."""
    try:
        conn = sqlite3.connect(db_file(), timeout=10)
        row = conn.execute("SELECT saved_at, data FROM traktor_state WHERE id = 1").fetchone()
        conn.close()
        if not row or time.time() - row[0] > TRAKTOR_RESTORE_MAX_AGE:
            return
        traktor_state.load(json.loads(row[1]))
        loaded = sum(1 for d in traktor_state.decks.values() if d["restored"])
        print(f"Restored Traktor state: {loaded} deck(s) loaded")
    except Exception as exc:
        print(f"Could not restore Traktor state: {exc}")


async def push_traktor_state(dirty: asyncio.Event):
    """Coalesce state changes into SSE events (at most one per interval) and persist them."""
    last_saved, unsaved = 0.0, False
    while True:
        try:
            await asyncio.wait_for(dirty.wait(), timeout=1.0)
            dirty.clear()
            await broadcast(traktor_event())
            unsaved = True
            decks = {k: dict(v) for k, v in traktor_state.decks.items()}  # copied here, on the loop thread
            if await run_in_threadpool(sync_queue_with_traktor, decks, time.time()):
                await broadcast({"event": "queue_update", "status": "traktor"})
            await asyncio.sleep(TRAKTOR_PUSH_INTERVAL)
        except asyncio.TimeoutError:
            pass
        if unsaved and time.monotonic() - last_saved >= TRAKTOR_SAVE_INTERVAL:
            await run_in_threadpool(save_traktor_state)
            last_saved, unsaved = time.monotonic(), False


async def broadcast(data):
    if not subscribers:
        return
    if data.get("event") != "traktor_state":
        print(f"Broadcasting event: {data}")
    message = f"data: {json.dumps(data)}\n\n"
    for q in list(subscribers):
        await q.put(message)

def init_db():
    conn = sqlite3.connect(db_file(), timeout=10)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS traktor_state
                 (id INTEGER PRIMARY KEY CHECK (id = 1), saved_at REAL, data TEXT)''')
    queue_store.ensure_schema(conn)
    conn.commit()
    conn.close()

def notify(notification: dict):
    """Push an SSE event from a worker thread onto the event loop."""
    if loop is not None:
        asyncio.run_coroutine_threadsafe(broadcast(notification), loop)

class YtBackend:
    """What the queue worker needs from YouTube: download a video's audio as mp3."""

    def download(self, video_id, dest_dir):
        opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}],
            'outtmpl': f'{dest_dir}/audio.%(ext)s',
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://music.youtube.com/watch?v={video_id}"])
        out = Path(dest_dir) / "audio.mp3"
        if not out.is_file():
            raise RuntimeError("the download produced no mp3 file")
        return out


async def on_queue_result(item_id, outcome):
    """A queued rip finished (ready / failed / removed): tell the page, which shows the toast."""
    def finish():
        conn = queue_store.open_db(db_file())
        try:
            return queue_store.get_item(conn, item_id)
        finally:
            conn.close()

    item = await run_in_threadpool(finish)
    if item and outcome in ("ready", "failed"):
        await broadcast({"event": "rip_result", "kind": "new", "id": item_id, "ok": outcome == "ready",
                         "video_id": item["video_id"], "title": item["title"], "artist": item["artist"],
                         "message": item["error"] if outcome == "failed" else None})
    await broadcast({"event": "queue_update", "id": item_id, "status": outcome})


queue_worker = QueueWorker(db_file, queue_store.queue_dir, YtBackend(), on_queue_result)


def public_item(item):
    """An item for the pages: never the full file path (it contains the user's folder names)."""
    keep = ("id", "session_id", "position", "source", "artist", "title", "album", "video_id", "status",
            "length_s", "bpm", "error", "added_at", "loaded_at", "played_at")
    out = {k: item[k] for k in keep}
    out["file_name"] = file_name_of(item["file_path"])
    return out


def reveal_folder(path: str):
    """Open a folder in the platform's file manager."""
    if sys.platform.startswith("win"):
        os.startfile(os.path.abspath(path))  # noqa: S606 (Windows only)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)

@app.get("/")
async def home():
    return RedirectResponse("/dj")  # temporary: the one-page UI replaces this in pass 2

@app.get("/booth", response_class=HTMLResponse)
async def booth_view(request: Request):
    return templates.TemplateResponse(request=request, name="booth.html")

@app.get("/dj", response_class=HTMLResponse)
async def dj_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dj.html")

def queue_state(conn):
    """Tonight's session; the QUEUE (what is still to play, in order, plus failed rips); the tracks already
    played tonight; and whether Traktor needs the folder re-selected. A played track leaves the queue here only:
    its file stays in tonight's folder."""
    session = queue_store.current_session(conn)
    items = queue_store.list_items(conn, session["id"])
    queue = [i for i in items if i["status"] != "played"]
    return {
        "session": session,
        "items": [public_item(i) for i in queue],
        "to_play": sum(1 for i in queue if i["status"] in ("queued", "downloading", "ready")),
        "played": [public_item(i) for i in queue_store.list_played(conn, session["id"])],
        "folder": queue_store.folder_state(conn),
    }


def queue_action(action, changed=False):
    """Run action(conn) against the queue DB and answer with the fresh queue state.
    changed=True tells the open pages to refresh."""
    conn = queue_store.open_db(db_file())
    try:
        error = action(conn)
        if error:
            return JSONResponse({"status": "error", "message": error}, status_code=404)
        if changed:
            notify({"event": "queue_update", "status": "changed"})
        return JSONResponse(queue_state(conn))
    finally:
        conn.close()


@app.get("/api/queue_items")
def get_queue_items():
    return queue_action(lambda conn: None)

@app.post("/api/rip")
def rip_track(video_id: str = Form(...), artist: str = Form(...), title: str = Form(...),
              album: str | None = Form(None), thumbnail: str | None = Form(None)):
    """Rip one track (the only way anything is downloaded). Answers with what happened: new (downloading now),
    duplicate (already in the library: linked into tonight's folder, never downloaded again), in_queue (already
    in tonight's queue: nothing changed) or error."""
    conn = queue_store.open_db(db_file())
    try:
        outcome = queue_rip.request_rip(conn, video_id=video_id, artist=artist, title=title,
                                        album=album or None, thumbnail=thumbnail or None, root=queue_store.queue_dir())
    finally:
        conn.close()
    kind = outcome["kind"]
    if kind == "new":
        queue_worker.wake_threadsafe()
    if kind in ("new", "duplicate", "error"):
        notify({"event": "queue_update", "status": "added"})
    return JSONResponse({"kind": kind, "item": public_item(outcome["item"]), "message": outcome.get("message"),
                         "cover": f"/api/library/cover/{video_id}" if kind == "duplicate" else None})

@app.get("/api/library/cover/{video_id}")
def library_cover(video_id: str):
    """The cover art embedded in the library file for this video (the page falls back to its own picture)."""
    conn = queue_store.open_db(db_file())
    try:
        entry = library.find(conn, video_id)
    finally:
        conn.close()
    cover = queue_files.read_cover(entry["path"]) if entry else None
    if not cover:
        return JSONResponse({"status": "no cover"}, status_code=404)
    return Response(content=cover[0], media_type=cover[1], headers={"Cache-Control": "max-age=3600"})

@app.get("/api/search")
def universal_search(q: str):
    """Search YouTube Music (songs first, then anything), or look up a pasted YouTube / YouTube Music link.
    Each result says whether that exact video is already in the library."""
    yt = get_ytmusic()
    video_id = queue_search.video_id_from_text(q)
    if video_id:
        found = queue_search.normalize_song(yt.get_song(video_id))
        results = [found] if found else []
    else:
        raw = yt.search(q, filter="songs", limit=8) or yt.search(q, limit=8)
        results = [r for r in (queue_search.normalize_result(x) for x in raw[:8]) if r]
    conn = queue_store.open_db(db_file())
    try:
        known = library.known_ids(conn, [r["video_id"] for r in results])
    finally:
        conn.close()
    for r in results:
        r["in_library"] = r["video_id"] in known
    return JSONResponse(results)

@app.post("/api/queue_items/move")
def move_queue_item(id: int = Form(...), direction: str = Form(...)):
    def act(conn):
        if not queue_store.get_item(conn, id):
            return "no such item"
        if queue_store.move_item(conn, id, direction):  # swaps with the neighbouring queued/ready item
            queue_files.sync_markers(conn, queue_store.queue_dir())  # busy files are retried by the worker
    return queue_action(act, changed=True)

@app.post("/api/queue_items/remove")
def remove_queue_item(id: int = Form(...)):
    def act(conn):
        item = queue_store.get_item(conn, id)
        if not item:
            return "no such item"
        if item["status"] != "removed":  # the file, if any, stays where it is: Traktor may have it loaded
            queue_store.update_item(conn, id, status="removed")
    return queue_action(act, changed=True)

@app.post("/api/queue_items/retry")
def retry_queue_item(id: int = Form(...)):
    def act(conn):
        item = queue_store.get_item(conn, id)
        if not item:
            return "no such item"
        if item["status"] == "failed":
            queue_store.update_item(conn, id, status="queued", error=None)
            queue_worker.wake_threadsafe()
    return queue_action(act, changed=True)

@app.post("/api/queue_items/new_night")
def new_night():
    return queue_action(lambda conn: queue_store.new_night(conn) and None, changed=True)

@app.post("/api/queue_items/refreshed")
def queue_folder_refreshed():
    """The DJ re-selected Ripper Queue in Traktor: clear the hint."""
    return queue_action(lambda conn: queue_store.ack_folder(conn), changed=True)

@app.post("/api/traktor/reset")
async def reset_traktor_state():
    """Forget the deck state (and the copy saved in the database), e.g. after a test run left fake decks.
    Tracks already marked played stay played."""
    traktor_state.reset()
    await run_in_threadpool(save_traktor_state)
    await broadcast(traktor_event())
    return JSONResponse({"status": "success"})

@app.get("/api/traktor/state")
async def get_traktor_state():
    return JSONResponse(traktor_state.snapshot())

@app.get("/api/events")
async def events(request: Request):
    async def event_generator():
        q = asyncio.Queue()
        subscribers.add(q)
        try:
            yield f"data: {json.dumps(traktor_event())}\n\n"  # current state for a new client
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Use a timeout so we can check for disconnection
                    message = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield message
                except asyncio.TimeoutError:
                    continue
        finally:
            subscribers.discard(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/open_folder")
def open_folder(folder_type: str = Form(...)):
    try:
        target = str(queue_store.queue_dir())  # one folder for everything: Ripper Queue (folder_type is kept for compatibility)
        os.makedirs(target, exist_ok=True)
        reveal_folder(target)
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
