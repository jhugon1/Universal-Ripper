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
from ripper_queue import playlists as queue_playlists
from ripper_queue import rip as queue_rip
from ripper_queue import spotify as queue_spotify
from ripper_queue import search as queue_search
from ripper_queue import store as queue_store
from ripper_queue import sync as queue_sync
from ripper_queue.worker import QueueWorker
from traktor.receiver import TraktorReceiver, configured_port
from traktor.state import TraktorState, file_name_of
import asyncio
import json
import re
import uuid
from html import escape as html_escape
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
    playlist_fetches.clear()  # a fresh start has no fetched playlists and no running job
    playlist_job["job"] = None
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
    conn.execute(queue_spotify.SCHEMA)
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

@app.get("/", response_class=HTMLResponse)
async def page(request: Request):
    """The one page: decks on top, the ripper bottom left, tonight's queue bottom right."""
    return templates.TemplateResponse(request=request, name="app.html", context={"dev": devreload.is_dev()})


def queue_state(conn):
    """Tonight's session; the QUEUE (what is still to play, in order, plus failed rips); the tracks already
    played tonight; and whether Traktor needs the folder re-selected. A played track leaves the queue here only:
    its file stays in tonight's folder."""
    session = queue_store.current_session(conn)
    queue, played = queue_store.split_queue(queue_store.list_items(conn, session["id"]))
    return {
        "session": session,
        "items": [public_item(i) for i in queue],
        "to_play": sum(1 for i in queue if i["status"] in ("queued", "downloading", "ready")),
        "played": [public_item(i) for i in played],
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

# ---- playlist rips: fetch a playlist link, then rip it (progress comes over SSE) ----
playlist_fetches = {}   # fetch id -> fetched playlist (the last few), so ripping does not fetch it again
playlist_job = {"job": None}


def spotify_client_id(conn):
    """The user's Spotify Client ID: RIPPER_SPOTIFY_CLIENT_ID, or the one pasted into the page."""
    return (os.environ.get("RIPPER_SPOTIFY_CLIENT_ID") or "").strip() or (queue_store.meta_get(conn, "spotify_client_id") or "").strip() or None


def spotify_fetch(url):
    """Read a Spotify playlist with the user's own login (raises SpotifyError saying what is missing)."""
    conn = queue_store.open_db(db_file())
    try:
        token = queue_spotify.access_token(conn, spotify_client_id(conn), http=queue_spotify.http_json)
    finally:
        conn.close()
    return queue_spotify.fetch_playlist(url, token, http=queue_spotify.http_json)


@app.post("/api/playlist/fetch")
def playlist_fetch(url: str = Form(...)):
    """Name, track count, total length and destination folder of an Apple Music or Spotify playlist link."""
    try:
        result = queue_playlists.fetch_playlist(url, spotify=spotify_fetch)
    except queue_playlists.PlaylistError as exc:
        return JSONResponse({"message": str(exc)}, status_code=400)
    except queue_spotify.SpotifyError as exc:  # `action` tells the page whether to offer setup or the login
        return JSONResponse({"message": str(exc), "action": exc.action}, status_code=400)
    fetch_id = uuid.uuid4().hex[:8]
    playlist_fetches[fetch_id] = result
    while len(playlist_fetches) > 10:
        playlist_fetches.pop(next(iter(playlist_fetches)))
    dest = queue_playlists.playlist_dir() / result["folder_name"]
    existing = sum(1 for p in dest.glob("*.mp3")) if dest.is_dir() else 0
    shown = {k: v for k, v in result.items() if k != "tracks"}
    shown["tracks"] = [{k: t[k] for k in ("position", "artist", "title", "duration_s")} for t in result["tracks"]]
    return JSONResponse({**shown, "fetch_id": fetch_id, "dest": str(dest), "existing": existing})


spotify_pending = {}   # login state -> (code verifier, Client ID), until Spotify sends the user back


def spotify_state():
    conn = queue_store.open_db(db_file())
    try:
        cid = spotify_client_id(conn)
        saved = queue_spotify.load_tokens(conn)
    finally:
        conn.close()
    return {"client_id_set": bool(cid), "connected": bool(cid and saved and saved.get("refresh_token") and saved.get("client_id") == cid),
            "redirect_uri": queue_spotify.redirect_uri()}


@app.get("/api/spotify/status")
def spotify_status():
    return JSONResponse(spotify_state())


@app.post("/api/spotify/client_id")
def spotify_set_client_id(client_id: str = Form(...)):
    client_id = client_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", client_id):
        return JSONResponse({"message": "That does not look like a Spotify Client ID (letters and digits, about 32 characters)."}, status_code=400)
    conn = queue_store.open_db(db_file())
    try:
        queue_store.meta_set(conn, "spotify_client_id", client_id)
    finally:
        conn.close()
    return JSONResponse(spotify_state())


@app.get("/api/spotify/login")
def spotify_login():
    """Send the browser to Spotify's one-time login. It comes back to /api/spotify/callback."""
    conn = queue_store.open_db(db_file())
    try:
        cid = spotify_client_id(conn)
    finally:
        conn.close()
    if not cid:
        return JSONResponse({"message": "Add your Spotify Client ID first.", "action": "setup"}, status_code=400)
    verifier, state = queue_spotify.new_verifier(), uuid.uuid4().hex
    spotify_pending[state] = (verifier, cid)
    while len(spotify_pending) > 5:
        spotify_pending.pop(next(iter(spotify_pending)))
    return RedirectResponse(queue_spotify.authorize_url(cid, queue_spotify.redirect_uri(), state, queue_spotify.code_challenge(verifier)))


@app.get("/api/spotify/callback", response_class=HTMLResponse)
def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    pending = spotify_pending.pop(state or "", None)
    if error or not code or not pending:
        message, ok = f"Spotify did not connect ({error or 'the login expired'}). Close this window and try again.", False
    else:
        conn = queue_store.open_db(db_file())
        try:
            queue_spotify.exchange_code(conn, pending[1], code, pending[0], queue_spotify.redirect_uri(), http=queue_spotify.http_json)
            message, ok = "Spotify is connected. You can close this window.", True
        except queue_spotify.SpotifyError as exc:
            message, ok = f"{exc} Close this window and try again.", False
        finally:
            conn.close()
    if ok:
        notify({"event": "spotify_connected"})
    page = ('<!doctype html><meta charset="utf-8"><title>Spotify</title><body style="margin:0;background:#0E0F12;color:#ECEDEE;'
            'font:18px Inter,system-ui,sans-serif;display:grid;place-items:center;height:100vh">'
            f'<p style="max-width:420px;text-align:center">{html_escape(message)}</p></body>')
    return HTMLResponse(page, status_code=200 if ok else 400)


@app.post("/api/spotify/disconnect")
def spotify_disconnect():
    conn = queue_store.open_db(db_file())
    try:
        queue_spotify.forget_tokens(conn)
    finally:
        conn.close()
    return JSONResponse(spotify_state())


async def run_playlist(job):
    last = [0.0]

    def progress(j):  # called from worker threads
        now = time.monotonic()
        if j.status == "running" and now - last[0] < 0.4:
            return
        last[0] = now
        notify({"event": "playlist_progress", **j.snapshot()})

    try:
        await run_in_threadpool(queue_playlists.run_job, job, db_path=db_file(), backend=YtBackend(),
                                yt_factory=get_ytmusic, on_progress=progress)
    except Exception as exc:  # the job itself broke (not just one track)
        job.status = "done"
        job.problems.append(f"{type(exc).__name__}: {str(exc)[:120]}")
    await broadcast({"event": "playlist_done", **job.snapshot()})


@app.post("/api/playlist/rip")
async def playlist_rip(fetch_id: str = Form(...)):
    """Rip a fetched playlist into Playlist Rips/<Name> (<length>)/NN Artist - Title.mp3. If that folder exists it is
    updated in place. Not added to tonight's queue."""
    result = playlist_fetches.get(fetch_id)
    if not result:
        return JSONResponse({"message": "Fetch the playlist again first."}, status_code=404)
    running = playlist_job["job"]
    if running and running.status == "running":
        return JSONResponse({"message": "A playlist is already being ripped.", **running.snapshot()}, status_code=409)
    job = queue_playlists.PlaylistJob(result)
    playlist_job["job"] = job
    asyncio.create_task(run_playlist(job))
    return JSONResponse(job.snapshot())


@app.get("/api/playlist/job")
async def playlist_job_state():
    job = playlist_job["job"]
    return JSONResponse(job.snapshot() if job else {"status": "idle"})


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
        # the decks are copied here so a track loaded a moment ago is never deleted before the sync notices it
        decks = {k: dict(v) for k, v in traktor_state.decks.items()}
        result = queue_rip.remove_from_queue(conn, id, decks, queue_store.queue_dir())
        return result.get("error")
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
        # tonight's folder if it exists, else the Ripper Queue root (folder_type is kept for compatibility)
        conn = queue_store.open_db(db_file())
        try:
            tonight = queue_store.session_dir(queue_store.current_session(conn))
        finally:
            conn.close()
        target = str(tonight if tonight.is_dir() else queue_store.queue_dir())
        os.makedirs(target, exist_ok=True)
        reveal_folder(target)
        return JSONResponse({"status": "success"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
