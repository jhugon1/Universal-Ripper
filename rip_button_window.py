"""The floating RIP button: a small borderless window that sits on Traktor's window (top right, in its header row).

Run by run.py as its own process (never imported by the app):
    python rip_button_window.py [--parent PID] [--url URL] [--always]
Clicking it opens a drop-down search-and-rip panel (rip_panel.py) under it; the panel has an "Open full Ripper" link that shows the Ripper window (the same page as always).
Traktor is made its owner window, so it stays visible above Traktor (behind other apps) and hides when Traktor is minimised; it follows Traktor when it moves or resizes,
and exits when the app that started it (--parent) is gone. Windows only (elsewhere it just exits).
"""
import argparse
import ctypes
import ctypes.wintypes
import os
import sys
import time
import tkinter

REFRESH_EVERY = 5    # ticks: every ~0.5 s everything is re-applied from scratch, whatever we believe the state is
FAST_MS = 10   # the follow timer
POLL_MS = 100   # the move hook does the fast work; this only notices Traktor opening, closing or being minimised
IMG_H = 24                     # height of the RIPPER wordmark on the button
PAD_X, PAD_Y = 10, 3
W, H = 0, 0                    # set from the picture at start-up
BG, BG_HOVER, EDGE, ACCENT = "#040201", "#1A1208", "#5A4512", "#FF6A3D"   # BG is the wordmark picture's own background
HERE = os.path.dirname(os.path.abspath(__file__))
RIPPER_TITLE = "Ripped Ripper"
TRAKTOR_TITLE = "Traktor Pro"
OFFSET_FROM_LEFT = 240   # just right of the "TRAKTOR Pro 4" logo (left-anchored like the logo)
OFFSET_FROM_TOP = 53     # puts the button on the header row's centre line

user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def find_windows(prefix):
    """[(hwnd, (left, top, width, height), iconic)] of visible-or-minimised top-level windows whose title starts with prefix."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        iconic = bool(user32.IsIconic(hwnd))
        if (user32.IsWindowVisible(hwnd) or iconic) and window_title(hwnd).startswith(prefix):
            r = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append((hwnd, (r.left, r.top, r.right - r.left, r.bottom - r.top), iconic))
        return True

    user32.EnumWindows(visit, 0)
    return found


def main_traktor_window():
    """(hwnd, rect) of Traktor's big visible window, or None (Traktor also keeps small hidden windows for its controller screens)."""
    big = [(h, r) for h, r, iconic in find_windows(TRAKTOR_TITLE) if not iconic and r[2] >= 800 and r[3] >= 500]
    return max(big, key=lambda t: t[1][2] * t[1][3]) if big else None


def button_position(rect, width=W):
    """Where the button goes for a Traktor window rect: in the header row, just right of the logo."""
    left, top, _, _ = rect
    return left + OFFSET_FROM_LEFT, top + OFFSET_FROM_TOP


def visible_for(foreground_title: str) -> bool:
    """True while Traktor, the Ripper window or our own button/panel is the window in front (only used to decide always-on-top
    in the fallback where Traktor could not be made the button's owner)."""
    return foreground_title.startswith((TRAKTOR_TITLE, RIPPER_TITLE, "tk"))


def parent_alive(pid):
    if not pid:
        return True
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102   # WAIT_TIMEOUT: still running
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def toggle_ripper(url):
    """Show the Ripper window (start it when it is not open); when it is already in front, minimise it and give Traktor focus back."""
    ripper = find_windows(RIPPER_TITLE)
    if not ripper:
        import run   # the launcher already knows how to open the page in a chromeless Chrome/Edge window
        run.open_page(url)
        return
    hwnd, _, iconic = ripper[0]
    if not iconic and user32.GetForegroundWindow() == hwnd:
        user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        traktor = main_traktor_window()
        if traktor:
            user32.SetForegroundWindow(traktor[0])
    else:
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def set_owner(button_hwnd, owner_hwnd):
    """Make Traktor's window the owner of the button: Windows then keeps the button above Traktor (and behind other apps),
    hides it while Traktor is minimised, and needs no always-on-top. True when it worked."""
    fn = user32.SetWindowLongPtrW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_void_p
    fn(button_hwnd, -8, owner_hwnd)   # GWLP_HWNDPARENT sets the OWNER of a top-level window
    return is_owned_by(button_hwnd, owner_hwnd)


def is_owned_by(hwnd, owner_hwnd):
    return bool(owner_hwnd) and user32.GetWindow(hwnd, 4) == owner_hwnd   # GW_OWNER


def button_hwnd(window):
    return user32.GetParent(window.winfo_id()) or window.winfo_id()


def move_button(hwnd, x, y):
    """Move the button window without touching its size, z-order or focus (SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)."""
    user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, 0x0001 | 0x0004 | 0x0010)


def window_rect(hwnd):
    r = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def log(text):
    """A tiny error log (rip_button.log in the RippedRipper folder under LOCALAPPDATA, trimmed when it grows) so a misbehaving button leaves a trace."""
    try:
        path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "RippedRipper", "rip_button.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 200_000:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


_HOOK_PROC = None   # kept alive for as long as the hook exists
_HOOKS = []         # handles of the installed hooks


def remove_move_hooks():
    """Unhook everything. Must happen before the process ends: a hook that is still installed would call back into a Python that
    is shutting down, which is a fatal error (and a crash report window)."""
    user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
    user32.UnhookWinEvent.restype = ctypes.c_bool
    while _HOOKS:
        user32.UnhookWinEvent(_HOOKS.pop())


def install_move_hook(on_move, pid=0):
    """Call on_move(hwnd) the moment any window moves or resizes (EVENT_OBJECT_LOCATIONCHANGE), so the button follows Traktor
    with no polling delay. The callback runs on this thread's message loop (tkinter's)."""
    global _HOOK_PROC
    proc_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_long, ctypes.c_long, ctypes.c_uint, ctypes.c_uint)

    def proc(hook, event, hwnd, id_object, id_child, thread, when):
        if id_object == 0 and hwnd:   # OBJID_WINDOW: the window itself, not one of its parts
            try:
                on_move(hwnd)
            except Exception as exc:   # a callback error must never reach Windows
                log(f"move hook error: {exc!r}")

    _HOOK_PROC = proc_type(proc)
    user32.SetWinEventHook.restype = ctypes.c_void_p
    user32.SetWinEventHook.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, proc_type, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    handle = user32.SetWinEventHook(0x800B, 0x800B, None, _HOOK_PROC, pid, 0, 0)   # WINEVENT_OUTOFCONTEXT; pid 0 = every process
    if handle:
        _HOOKS.append(handle)
    return handle


def make_tool_window(window):
    """Never takes focus and stays out of alt-tab, but still takes mouse clicks."""
    hwnd = button_hwnd(window)
    GWL_EXSTYLE, TOOLWINDOW, NOACTIVATE = -20, 0x80, 0x8000000
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | TOOLWINDOW | NOACTIVATE)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent", type=int, default=0, help="exit when this process (the app) is gone")
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--always", action="store_true", help="show even when Traktor is not in front (testing)")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        return 0

    from PIL import Image, ImageTk

    from rip_panel import RipPanel

    global W, H
    root = tkinter.Tk()
    root.overrideredirect(True)
    root.configure(bg=EDGE)
    mark = Image.open(os.path.join(HERE, "static", "ripper_wordmark.png")).convert("RGB")
    mark = mark.resize((round(mark.width * IMG_H / mark.height), IMG_H), Image.LANCZOS)
    photo = ImageTk.PhotoImage(mark)
    W, H = mark.width + 2 * PAD_X + 2, IMG_H + 2 * PAD_Y + 2
    label = tkinter.Label(root, image=photo, bg=BG, cursor="hand2", bd=0)
    label.image = photo
    label.pack(fill="both", expand=True, padx=1, pady=1)
    last_click = [0.0]

    def attach(top):   # the panel is owned by Traktor too, so it also stays above Traktor and behind other apps
        if state["owner"]:
            set_owner(button_hwnd(top), state["owner"])

    panel = RipPanel(root, args.url, lambda: toggle_ripper(args.url), attach, moves=lambda: state["moves"])

    def panel_spot(pos):
        return pos[0], pos[1] + H + 3

    def clicked(_event):   # bound ONCE, on the window: a click on a child widget also reaches it, so binding children too fired twice
        now = time.monotonic()
        if now - last_click[0] > 0.3 and state["pos"]:
            last_click[0] = now
            panel.toggle(*panel_spot(state["pos"]))

    root.bind("<Button-1>", clicked)
    root.bind("<Enter>", lambda e: label.configure(bg=BG_HOVER))
    root.bind("<Leave>", lambda e: label.configure(bg=BG))
    root.geometry(f"{W}x{H}+0+0")
    root.update_idletasks()
    make_tool_window(root)
    root.withdraw()
    state = {"shown": False, "pos": None, "owner": None, "owned": False, "hook_pid": None, "n": 0, "moves": 0,
             "bh": button_hwnd(root), "burst_until": 0.0, "panel_dirty": False}

    def tick():
        try:
            if not parent_alive(args.parent):
                remove_move_hooks()
                root.destroy()
                return
            bh = button_hwnd(root)
            traktor = main_traktor_window()          # None while Traktor is closed or minimised
            topmost = False
            if traktor is not None:
                hwnd, rect = traktor
                if state["owner"] != hwnd:
                    state["owner"] = hwnd
                    pid = ctypes.c_ulong(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if state["hook_pid"] != pid.value:   # listen only to Traktor's own process
                        state["hook_pid"] = pid.value
                        install_move_hook(follow, pid.value)
                owned = is_owned_by(bh, hwnd) or set_owner(bh, hwnd)   # checked every tick: re-owned if anything undid it
                if owned != state["owned"]:
                    log(f"owned by Traktor: {owned}")
                state["owned"] = owned
                if not owned:   # could not make Traktor the owner: still show the button, on top only while Traktor (or us) is in front
                    topmost = args.always or visible_for(window_title(user32.GetForegroundWindow()))
            state["n"] += 1
            if traktor is not None and state["n"] % REFRESH_EVERY == 0:   # periodic refresh: forget the cached position, re-own, re-show
                state["pos"] = None
                set_owner(bh, traktor[0])
                if root.state() != "normal":
                    root.deiconify()
                user32.ShowWindow(bh, 4)   # SW_SHOWNOACTIVATE
            if traktor is not None:
                pos = button_position(traktor[1])
                here = window_rect(bh)
                if pos != state["pos"] or (here and (here[0], here[1]) != pos):   # also checks where the button REALLY is, every tick
                    if pos != state["pos"]:
                        state["moves"] += 1
                    move_button(bh, *pos)
                    state["pos"] = pos
                    if panel.is_open():
                        panel.move(*panel_spot(pos))
                if not state["shown"]:
                    root.deiconify()
                    state["shown"] = True
                if not user32.IsWindowVisible(bh):   # Windows hid it behind Tk's back (owned windows follow their owner's state changes)
                    user32.ShowWindow(bh, 4)          # SW_SHOWNOACTIVATE
                    log("button was hidden by the system while Traktor is visible: shown again")
                root.attributes("-topmost", topmost)
            elif state["shown"]:
                panel.hide()
                root.withdraw()
                state["shown"] = False
        except Exception as exc:                       # never let one bad tick end the loop: a dead loop is a button that never comes back
            log(f"tick error: {exc!r}")
            try:
                if not root.winfo_exists():
                    return
            except tkinter.TclError:
                return
        root.after(POLL_MS, tick)

    def reposition_win32():
        """Move the button to where Traktor's window now says it should be. Win32 calls only, NO tkinter: the move hook calls this
        from inside tkinter's own message loop, where touching Tk again is re-entrant and can crash the interpreter."""
        hwnd = state["owner"]
        rect = window_rect(hwnd) if hwnd and state["shown"] else None
        if rect:
            pos = button_position(rect)
            if pos != state["pos"]:
                state["moves"] += 1
                move_button(state["bh"], *pos)
                state["pos"] = pos
                state["panel_dirty"] = True

    def follow(hwnd):   # the move hook: Traktor's window moved or resized
        if hwnd == state["owner"]:
            reposition_win32()
            state["burst_until"] = time.monotonic() + 0.15   # events can arrive in clumps while dragging: keep re-checking for a moment

    def fast():   # a normal Tk timer (safe to touch Tk here)
        try:
            if time.monotonic() < state["burst_until"]:
                reposition_win32()
            if state["panel_dirty"]:
                state["panel_dirty"] = False
                if state["pos"] and panel.is_open():
                    panel.move(*panel_spot(state["pos"]))
        except Exception as exc:
            log(f"fast timer error: {exc!r}")
        try:
            root.after(FAST_MS, fast)
        except tkinter.TclError:
            pass

    fast()
    tick()
    try:
        root.mainloop()
    finally:
        remove_move_hooks()
    return 0


if __name__ == "__main__":
    sys.exit(main())
