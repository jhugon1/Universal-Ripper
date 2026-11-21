import sqlite3
import os
import threading
import time
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from ytmusicapi import YTMusic
import yt_dlp
import devreload
from ripper_queue import files as queue_files
from ripper_queue import store as queue_store
from ripper_queue import sync as queue_sync
from ripper_queue.worker import QueueWorker
from traktor.receiver import TraktorReceiver, configured_port
from traktor.state import TraktorState, file_name_of
from filenames import ytdlp_outtmpl
import asyncio
import json
from fastapi.responses import StreamingResponse

import subprocess
import sys
from pathlib import Path
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop
    loop = asyncio.get_running_loop()
    init_db()
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
        receiver = TraktorReceiver(traktor_state, port, on_change=lambda changes: dirty.set())
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


SEARCH_DIR = "DJ_Search_Rips"
QUEUE_DIR = "Guest_Queue_Rips"

# SSE Notification System
subscribers = set()
loop = None


traktor_state = TraktorState()
TRAKTOR_PUSH_INTERVAL = 0.25  # at most 4 traktor_state events per second


def traktor_event():
    return {"event": "traktor_state", "state": traktor_state.snapshot()}


def sync_queue_with_traktor(decks, now):
    """Loaded / played / BPM updates for queue items, from what the decks report."""
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
    c.execute('''CREATE TABLE IF NOT EXISTS requests
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  artist TEXT, 
                  song TEXT, 
                  status TEXT DEFAULT 'pending', 
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS traktor_state
                 (id INTEGER PRIMARY KEY CHECK (id = 1), saved_at REAL, data TEXT)''')
    queue_store.ensure_schema(conn)
    conn.commit()
    conn.close()

def notify(notification: dict):
    """Push an SSE event from a worker thread onto the event loop."""
    if loop is not None:
        asyncio.run_coroutine_threadsafe(broadcast(notification), loop)

def download_song(query: str, download_dir: str, request_id: int = None):
    try:
        yt = get_ytmusic()
        # Find official song
        results = yt.search(query, filter="songs")
        if not results:
            results = yt.search(query)
        
        if not results:
            raise Exception(f"No results found for: {query}")

        best_match = results[0]
        video_id = best_match.get('videoId')
        if not video_id:
            raise Exception(f"Could not find videoId for {query}")
            
        url = f"https://music.youtube.com/watch?v={video_id}"
        match_title = best_match.get('title') or query
        match_artist = ", ".join(a['name'] for a in best_match.get('artists') or [] if a.get('name'))
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
            'outtmpl': ytdlp_outtmpl(download_dir, match_artist, match_title),
            'quiet': False,
            'no_warnings': True
        }
        
        os.makedirs(download_dir, exist_ok=True)
        print(f"Starting download: {query}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"Successfully downloaded: {query}")
        
        # Update database if this was a queue request
        if request_id:
            conn = sqlite3.connect(db_file(), timeout=10)
            c = conn.cursor()
            c.execute("UPDATE requests SET status='downloaded' WHERE id=?", (request_id,))
            conn.commit()
            conn.close()

        # Notify clients
        notification = {
            "event": "download_complete",
            "query": query,
            "request_id": request_id,
            "status": "success"
        }
        notify(notification)

    except Exception as e:
        print(f"Error downloading {query}: {e}")
        notification = {
            "event": "download_failed",
            "query": query,
            "request_id": request_id,
            "status": "error",
            "message": str(e)
        }
        notify(notification)

class YtBackend:
    """What the queue worker needs from YouTube: look a track up and download its audio."""

    def lookup(self, item):
        yt = get_ytmusic()
        query = item["query"] or f"{item['artist']} {item['title']}"
        results = yt.search(query, filter="songs") or yt.search(query)
        if not results:
            raise RuntimeError(f"No results found for: {query}")
        match = next((r for r in results if item["video_id"] and r.get("videoId") == item["video_id"]), None)
        if match is None and not item["video_id"]:
            match = results[0]
        video_id = item["video_id"] or (match or {}).get("videoId")
        if not video_id:
            raise RuntimeError(f"Could not find a video for: {query}")
        thumbs = (match or {}).get("thumbnails") or []
        return {"video_id": video_id, "album": ((match or {}).get("album") or {}).get("name"),
                "thumbnail": thumbs[-1].get("url") if thumbs else None}

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
    """A queue item finished (ready / failed / removed): update its request and tell the pages."""
    def finish():
        conn = queue_store.open_db(db_file())
        try:
            item = queue_store.get_item(conn, item_id)
            if item and item["request_id"] and outcome == "ready":
                conn.execute("UPDATE requests SET status='downloaded' WHERE id=?", (item["request_id"],))
                conn.commit()
            return item
        finally:
            conn.close()

    item = await run_in_threadpool(finish)
    if item and outcome in ("ready", "failed"):
        event = {"event": "download_complete" if outcome == "ready" else "download_failed",
                 "query": f"{item['artist']} {item['title']}", "request_id": item["request_id"],
                 "status": "success" if outcome == "ready" else "error"}
        if outcome == "failed":
            event["message"] = item["error"]
        await broadcast(event)
    await broadcast({"event": "queue_update", "id": item_id, "status": outcome})


queue_worker = QueueWorker(db_file, queue_store.queue_dir, YtBackend(), on_queue_result)


def add_queue_item(**fields):
    """Add an item to tonight's session and wake the worker. Returns the item."""
    fields.setdefault("query", f"{fields['artist']} {fields['title']}")
    conn = queue_store.open_db(db_file())
    try:
        item, created = queue_store.add_item(conn, **fields)
    finally:
        conn.close()
    if created:
        queue_worker.wake_threadsafe()
    return item


def public_item(item):
    """An item for the pages: never the full file path (it contains the user's folder names)."""
    keep = ("id", "session_id", "position", "source", "request_id", "artist", "title", "video_id", "status",
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

@app.get("/", response_class=HTMLResponse)
async def guest_view(request: Request):
    return templates.TemplateResponse(request=request, name="guest.html")

@app.get("/booth", response_class=HTMLResponse)
async def booth_view(request: Request):
    return templates.TemplateResponse(request=request, name="booth.html")

@app.get("/dj", response_class=HTMLResponse)
async def dj_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dj.html")

@app.post("/api/request")
def submit_request(artist: str = Form(...), song: str = Form(...)):
    query = f"{artist} {song}"
    yt = get_ytmusic()
    # Search for songs first
    results = yt.search(query, filter="songs", limit=1)
    
    final_artist = artist
    final_song = song
    
    if results:
        best_match = results[0]
        final_song = best_match.get('title', song)
        artists = best_match.get('artists', [])
        if artists:
            final_artist = ", ".join([a['name'] for a in artists])
    else:
        # Try generic search if no "songs" found
        results = yt.search(query, limit=1)
        if results:
            best_match = results[0]
            final_song = best_match.get('title', song)
            artists = best_match.get('artists', [])
            if artists and isinstance(artists, list):
                final_artist = artists[0].get('name', artist)

    conn = sqlite3.connect(db_file(), timeout=10)
    c = conn.cursor()
    c.execute("INSERT INTO requests (artist, song, status) VALUES (?, ?, 'pending')", (final_artist, final_song))
    conn.commit()
    conn.close()
    return JSONResponse({
        "status": "success", 
        "matched_artist": final_artist, 
        "matched_song": final_song
    })

@app.get("/api/queue")
def get_queue():
    conn = sqlite3.connect(db_file(), timeout=10)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM requests ORDER BY timestamp DESC")
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return JSONResponse(rows)

@app.post("/api/update_status")
def update_status(id: int = Form(...), status: str = Form(...)):
    conn = sqlite3.connect(db_file(), timeout=10)
    c = conn.cursor()
    c.execute("SELECT artist, song FROM requests WHERE id=?", (id,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE requests SET status=? WHERE id=?", (status, id))
        conn.commit()
        # Approving a request puts it in tonight's queue; the queue worker downloads it
        if status == 'selected':
            add_queue_item(source="request", request_id=id, artist=row[0], title=row[1])
    conn.close()
    return JSONResponse({"status": "success"})

def queue_state(conn):
    """Tonight's session, its items in order, and whether Traktor needs the folder re-selected."""
    session = queue_store.current_session(conn)
    nxt = queue_store.up_next(conn, session["id"])
    return {
        "session": session,
        "items": [public_item(i) for i in queue_store.list_items(conn, session["id"])],
        "folder": queue_store.folder_state(conn),
        "up_next": public_item(nxt) if nxt else None,
    }


def queue_action(action):
    """Run action(conn) against the queue DB and answer with the fresh queue state."""
    conn = queue_store.open_db(db_file())
    try:
        error = action(conn)
        if error:
            return JSONResponse({"status": "error", "message": error}, status_code=404)
        return JSONResponse(queue_state(conn))
    finally:
        conn.close()


@app.get("/api/queue_items")
def get_queue_items():
    return queue_action(lambda conn: None)

@app.post("/api/queue_items/add")
def add_queue_item_route(artist: str = Form(...), title: str = Form(...), video_id: str | None = Form(None)):
    return JSONResponse(public_item(add_queue_item(source="dj", artist=artist, title=title, video_id=video_id or None)))

@app.post("/api/queue_items/move")
def move_queue_item(id: int = Form(...), direction: str = Form(...)):
    def act(conn):
        if not queue_store.get_item(conn, id):
            return "no such item"
        if queue_store.move_item(conn, id, direction):  # swaps with the neighbouring queued/ready item
            queue_files.sync_markers(conn, queue_store.queue_dir())  # busy files are retried by the worker
    return queue_action(act)

@app.post("/api/queue_items/remove")
def remove_queue_item(id: int = Form(...)):
    def act(conn):
        item = queue_store.get_item(conn, id)
        if not item:
            return "no such item"
        if item["status"] != "removed":  # the file, if any, stays where it is: Traktor may have it loaded
            queue_store.update_item(conn, id, status="removed")
    return queue_action(act)

@app.post("/api/queue_items/retry")
def retry_queue_item(id: int = Form(...)):
    def act(conn):
        item = queue_store.get_item(conn, id)
        if not item:
            return "no such item"
        if item["status"] == "failed":
            queue_store.update_item(conn, id, status="queued", error=None)
            queue_worker.wake_threadsafe()
    return queue_action(act)

@app.post("/api/queue_items/new_night")
def new_night():
    return queue_action(lambda conn: queue_store.new_night(conn) and None)

@app.post("/api/queue_items/refreshed")
def queue_folder_refreshed():
    """The DJ re-selected Ripper Queue in Traktor: clear the hint."""
    return queue_action(lambda conn: queue_store.ack_folder(conn))

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

@app.post("/api/search")
def universal_search(query: str = Form(...)):
    yt = get_ytmusic()
    results = yt.search(query, filter="songs", limit=5)
    return JSONResponse(results)

@app.post("/api/download_direct")
def download_direct(background_tasks: BackgroundTasks, query: str = Form(...)):
    background_tasks.add_task(download_song, query, SEARCH_DIR)
    return JSONResponse({"status": "downloading"})

@app.post("/api/open_folder")
def open_folder(folder_type: str = Form(...)):
    try:
        target = SEARCH_DIR if folder_type == "search" else QUEUE_DIR
        os.makedirs(target, exist_ok=True)
        reveal_folder(target)
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/delete_request")
def delete_request(id: int = Form(...)):
    try:
        conn = sqlite3.connect(db_file(), timeout=10)
        c = conn.cursor()
        c.execute("DELETE FROM requests WHERE id=?", (id,))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/clear_queue")
def clear_queue():
    try:
        conn = sqlite3.connect(db_file(), timeout=10)
        c = conn.cursor()
        c.execute("DELETE FROM requests")
        conn.commit()
        conn.close()
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
