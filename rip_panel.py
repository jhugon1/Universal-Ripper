"""The drop-down search-and-rip panel that opens under the floating RIPPER button (see rip_button_window.py).

Native tkinter, no new dependencies. It talks to the running app's own HTTP API (/api/search, /api/rip, /api/queue_items), so
a rip started here is exactly the same as one started on the page: same library, same tonight's queue, same RIPPED pop-up.
Everything slow (search, covers, rips, progress) runs on worker threads and reports back through a queue that the Tk loop drains.
"""
import io
import json
import queue
import threading
import time
import tkinter
import urllib.error
import urllib.parse
import urllib.request

BG, ROW, ROW_HOVER, EDGE = "#1B1B1B", "#242424", "#2D2D2D", "#4A4A4A"
TEXT, SUB, ACCENT, OK, BAD = "#E6E6E6", "#8C8C8C", "#E0A030", "#58C07A", "#E8715A"
WIDTH, HEIGHT, THUMB = 480, 440, 40
FONT, FONT_BOLD, FONT_SMALL = ("Segoe UI", 10), ("Segoe UI Semibold", 10), ("Segoe UI", 9)
PROGRESS_POLL_S, PROGRESS_TIMEOUT_S = 1.5, 180


# ---- pure helpers (tested without a window) --------------------------------------------------
def mouse_button_down():
    """True while the left mouse button is held down (Windows; elsewhere always False)."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
    except Exception:
        return False


def format_length(seconds):
    """3:45 for 225 seconds; '' when unknown."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}:{total % 60:02d}" if total > 0 else ""


def subtitle(result):
    """'Artist · Album · 3:45' without the parts that are missing."""
    parts = [result.get("artist"), result.get("album"), format_length(result.get("length_s"))]
    return " · ".join(p for p in parts if p)


def ellipsize(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def item_status(payload, item_id):
    """The status of one queue item in a /api/queue_items answer (it may be in the queue, to play, or already played)."""
    for key in ("items", "to_play", "played"):
        for item in (payload or {}).get(key) or []:
            if item.get("id") == item_id:
                return item.get("status"), item.get("error")
    return None, None


def rip_outcome(kind, message=None):
    """What the Rip button should show for the answer of POST /api/rip: (label, colour, finished)."""
    if kind == "new":
        return "Downloading…", ACCENT, False
    if kind == "done":
        return "Ripped ✓", OK, True
    if kind == "duplicate":
        return "Added ✓", OK, True
    if kind == "in_queue":
        return "In queue ✓", OK, True
    return ellipsize(message or "Failed", 28), BAD, True


class ApiError(Exception):
    pass


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def _open(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("message") or exc.reason
            except Exception:
                detail = exc.reason
            raise ApiError(str(detail)) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ApiError("The Ripper app is not reachable") from None

    def get(self, path, timeout=25):
        return self._open(urllib.request.Request(self.base + path), timeout)

    def post_form(self, path, fields, timeout=25):
        data = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode()
        return self._open(urllib.request.Request(self.base + path, data), timeout)


# ---- the panel --------------------------------------------------------------------------------
class RipPanel:
    def __init__(self, root, api_base, open_full, attach_window=None, moves=None):
        self.root, self.api, self.open_full, self.attach_window = root, Api(api_base), open_full, attach_window
        self.moves = moves   # a counter that goes up whenever Traktor's window moves or resizes
        self.events = queue.Queue()
        self.rows = {}       # video_id -> {"thumb": Label, "button": Button, "note": Label}
        self.photos = {}     # keep PhotoImages alive
        self.search_id = 0
        self.closed_at = 0.0
        self.top = tkinter.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.configure(bg=EDGE)
        self.top.geometry(f"{WIDTH}x{HEIGHT}")
        self._build()
        self.top.bind("<Escape>", lambda e: self.hide())
        self.top.bind("<FocusOut>", self._on_focus_out)
        self.root.after(40, self._drain)

    # -- layout
    def _build(self):
        body = tkinter.Frame(self.top, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        bar = tkinter.Frame(body, bg=BG)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        self.entry = tkinter.Entry(bar, bg="#2A2A2A", fg=TEXT, insertbackground=TEXT, relief="flat", font=("Segoe UI", 11),
                                   highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry.bind("<Return>", lambda e: self.search())
        go = tkinter.Button(bar, text="Search", command=self.search, bg="#333333", fg=TEXT, activebackground="#444444", activeforeground=TEXT,
                            relief="flat", font=FONT_BOLD, padx=12, cursor="hand2", bd=0)
        go.pack(side="left", padx=(8, 0), ipady=3)

        self.status = tkinter.Label(body, text="Search for a song, or paste a YouTube link.", bg=BG, fg=SUB, font=FONT_SMALL, anchor="w")
        self.status.pack(fill="x", padx=12)

        holder = tkinter.Frame(body, bg=BG)
        holder.pack(fill="both", expand=True, padx=(10, 4), pady=(6, 4))
        self.canvas = tkinter.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        bar_y = tkinter.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar_y.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar_y.pack(side="right", fill="y")
        self.list = tkinter.Frame(self.canvas, bg=BG)
        self.window = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.top.bind_all("<MouseWheel>", self._wheel)

        foot = tkinter.Frame(body, bg=BG)
        foot.pack(fill="x", padx=10, pady=(0, 8))
        full = tkinter.Button(foot, text="Open full Ripper  ↗", command=self._open_full, bg=BG, fg=SUB, activebackground=BG,
                              activeforeground=TEXT, relief="flat", font=FONT_SMALL, cursor="hand2", bd=0)
        full.pack(side="right")

    def _wheel(self, event):
        if self.is_open():
            self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    # -- showing and hiding
    def is_open(self):
        return self.top.state() != "withdrawn"

    def show(self, x, y):
        self.move(x, y)
        self.top.deiconify()
        self.top.lift()
        if self.attach_window:
            self.attach_window(self.top)
        self.top.update_idletasks()
        try:   # an undecorated window needs the foreground (we have just been clicked, so Windows allows it) before it takes typing
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.top.winfo_id()) or self.top.winfo_id()
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.top.focus_force()
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def move(self, x, y):
        self.top.geometry(f"{WIDTH}x{HEIGHT}+{int(x)}+{int(y)}")

    def hide(self):
        if self.is_open():
            self.top.withdraw()
            self.closed_at = time.monotonic()

    def toggle(self, x, y):
        if self.is_open():
            self.hide()
        elif time.monotonic() - self.closed_at > 0.35:   # a click that just closed it by taking focus must not reopen it
            self.show(x, y)

    def _on_focus_out(self, _event=None):
        self.top.after(150, self._lost_focus, self.moves() if self.moves else 0)

    def _lost_focus(self, moves_at_loss=0):
        """Focus left the panel. Clicking somewhere else closes it, but grabbing Traktor's title bar to move or resize the window
        also takes focus: while the mouse button is still down, and if the window moved, the panel stays open (and follows it)."""
        if not self.is_open() or self.top.focus_displayof() is not None:
            return
        if mouse_button_down():
            self.top.after(150, self._lost_focus, moves_at_loss)   # maybe a drag: decide when the button is released
            return
        if self.moves and self.moves() != moves_at_loss:
            return
        self.hide()

    def _open_full(self):
        self.hide()
        self.open_full()

    # -- search
    def search(self):
        query = self.entry.get().strip()
        if not query:
            return
        self.search_id += 1
        mine = self.search_id
        self._clear()
        self._say("Searching…", SUB)

        def work():
            try:
                results = self.api.get("/api/search?q=" + urllib.parse.quote(query))
                self.events.put(("results", mine, results))
            except ApiError as exc:
                self.events.put(("error", mine, str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _say(self, text, colour=SUB):
        self.status.configure(text=ellipsize(text, 70), fg=colour)

    def _clear(self):
        for child in self.list.winfo_children():
            child.destroy()
        self.rows.clear()
        self.canvas.yview_moveto(0)

    def _show_results(self, results):
        self._clear()
        if not results:
            self._say("No results.", SUB)
            return
        self._say(f"{len(results)} results. Rip adds a track to tonight's queue.", SUB)
        for r in results[:8]:
            self._add_row(r)

    def _add_row(self, r):
        row = tkinter.Frame(self.list, bg=ROW)
        row.pack(fill="x", pady=2, padx=(0, 6))
        thumb = tkinter.Label(row, bg="#2A2A2A", width=5, height=2)
        thumb.pack(side="left", padx=(6, 8), pady=6)
        text = tkinter.Frame(row, bg=ROW)
        text.pack(side="left", fill="x", expand=True)
        tkinter.Label(text, text=ellipsize(r.get("title"), 34), bg=ROW, fg=TEXT, font=FONT_BOLD, anchor="w").pack(fill="x")
        sub = subtitle(r) + ("  ·  In library" if r.get("in_library") else "")
        tkinter.Label(text, text=ellipsize(sub, 48), bg=ROW, fg=SUB, font=FONT_SMALL, anchor="w").pack(fill="x")
        button = tkinter.Button(row, text="Rip", width=11, bg=ACCENT, fg="#1B1B1B", activebackground="#F0B850", relief="flat",
                                font=FONT_BOLD, cursor="hand2", bd=0, command=lambda: self._rip(r))
        button.pack(side="right", padx=8, ipady=2)
        self.rows[r["video_id"]] = {"thumb": thumb, "button": button}
        for w in (row, text):
            w.bind("<Enter>", lambda e, f=row, t=text: (f.configure(bg=ROW_HOVER), t.configure(bg=ROW_HOVER)))
            w.bind("<Leave>", lambda e, f=row, t=text: (f.configure(bg=ROW), t.configure(bg=ROW)))
        if r.get("thumbnail"):
            threading.Thread(target=self._fetch_thumb, args=(r["video_id"], r["thumbnail"], self.search_id), daemon=True).start()

    def _fetch_thumb(self, video_id, url, mine):
        try:
            from PIL import Image
            with urllib.request.urlopen(url, timeout=6) as resp:
                img = Image.open(io.BytesIO(resp.read())).convert("RGB")
            side = min(img.size)
            left, top = (img.width - side) // 2, (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side)).resize((THUMB, THUMB), Image.LANCZOS)
            self.events.put(("thumb", mine, (video_id, img)))
        except Exception:
            pass   # no cover is fine

    # -- ripping
    def _rip(self, r):
        row = self.rows.get(r["video_id"])
        if not row:
            return
        row["button"].configure(text="…", state="disabled")

        def work():
            try:
                answer = self.api.post_form("/api/rip", {"video_id": r["video_id"], "artist": r.get("artist") or "", "title": r.get("title") or "",
                                                         "album": r.get("album"), "thumbnail": r.get("thumbnail")})
            except ApiError as exc:
                self.events.put(("ripped", r["video_id"], ("error", str(exc), None)))
                return
            self.events.put(("ripped", r["video_id"], (answer.get("kind"), answer.get("message"), (answer.get("item") or {}).get("id"))))
            if answer.get("kind") == "new" and (answer.get("item") or {}).get("id") is not None:
                self._watch(r["video_id"], answer["item"]["id"])

        threading.Thread(target=work, daemon=True).start()

    def _watch(self, video_id, item_id):
        """Follow a download until the app says it is ready or failed (the app's own pop-up announces the finished rip)."""
        deadline = time.monotonic() + PROGRESS_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(PROGRESS_POLL_S)
            try:
                status, error = item_status(self.api.get("/api/queue_items", timeout=10), item_id)
            except ApiError:
                continue
            if status in ("ready", "loaded", "played"):
                self.events.put(("ripped", video_id, ("done", None, None)))
                return
            if status in ("failed", "removed"):
                self.events.put(("ripped", video_id, ("error", error or "Download failed", None)))
                return

    # -- results from the worker threads, on the Tk thread
    def _drain(self):
        try:
            while True:
                kind, key, payload = self.events.get_nowait()
                if kind == "results" and key == self.search_id:
                    self._show_results(payload)
                elif kind == "error" and key == self.search_id:
                    self._clear()
                    self._say("Search did not work: " + str(payload), BAD)
                elif kind == "thumb" and key == self.search_id:
                    from PIL import ImageTk
                    video_id, img = payload
                    row = self.rows.get(video_id)
                    if row:
                        photo = ImageTk.PhotoImage(img)
                        self.photos[video_id] = photo
                        row["thumb"].configure(image=photo, width=THUMB, height=THUMB)
                elif kind == "ripped":
                    row = self.rows.get(key)
                    if row:
                        outcome, message, _ = payload
                        label, colour, finished = rip_outcome(outcome, message)
                        row["button"].configure(text=label, bg=ROW if finished else ACCENT, fg=colour if finished else "#1B1B1B", state="disabled")
        except queue.Empty:
            pass
        except tkinter.TclError:
            return
        self.root.after(40, self._drain)
