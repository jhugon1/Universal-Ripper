"""The "RIPPED" pop-up shown over Traktor: a small borderless always-on-top window that fades in, waits and fades out.

Run by ripper_queue/popup.py as its own process (never imported by the app):
    python popup_window.py --title "Artist - Title" [--lead Already] [--sub "Added to tonight's queue"]
It does not take focus and mouse clicks go through it, so it never gets in the way of the DJ. It is placed over the
Traktor window when one is found, otherwise in the middle of the screen. Windows only (elsewhere it just exits).
"""
import argparse
import ctypes
import os
import sys
import tkinter

HOLD_MS = 2300
FADE_IN_MS = 160
FADE_OUT_MS = 650
STEP_MS = 16
WIDTH, HEIGHT = 560, 118
BG = "#101010"
EDGE = "#5A4512"
HERE = os.path.dirname(os.path.abspath(__file__))


def traktor_rect():
    """(left, top, width, height) of the visible Traktor window, or None."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith("Traktor Pro"):
                rect = ctypes.wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                found.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def make_click_through(window):
    """Layered, transparent to the mouse, never activated, hidden from alt-tab."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    GWL_EXSTYLE, LAYERED, TRANSPARENT, TOOLWINDOW, NOACTIVATE = -20, 0x80000, 0x20, 0x80, 0x8000000
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE)


def ellipsize(text, limit=48):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--lead", default="")
    ap.add_argument("--sub", default="")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        return 0
    import ctypes.wintypes  # noqa: F401  (RECT)
    from PIL import Image, ImageTk

    root = tkinter.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.0)
    root.configure(bg=EDGE)
    inner = tkinter.Frame(root, bg=BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    head = tkinter.Frame(inner, bg=BG)
    head.pack(anchor="w", padx=18, pady=(14, 0))
    if args.lead:
        tkinter.Label(head, text=args.lead, bg=BG, fg="#ECEDEE", font=("Segoe UI Semibold", 22)).pack(side="left", padx=(0, 10))
    mark = Image.open(os.path.join(HERE, "static", "ripped_wordmark.png")).convert("RGBA")
    height = 46
    mark = mark.resize((round(mark.width * height / mark.height), height), Image.LANCZOS)
    photo = ImageTk.PhotoImage(mark)
    tkinter.Label(head, image=photo, bg=BG).pack(side="left")
    tkinter.Label(inner, text=ellipsize(args.title), bg=BG, fg="#ECEDEE", font=("Segoe UI Semibold", 15), anchor="w").pack(fill="x", padx=18, pady=(6, 0))
    if args.sub:
        tkinter.Label(inner, text=args.sub, bg=BG, fg="#9BA0A8", font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=18)

    rect = traktor_rect()
    if rect:
        left, top, width, height_ = rect
        x, y = left + (width - WIDTH) // 2, top + max(60, height_ // 5)
    else:
        x, y = (root.winfo_screenwidth() - WIDTH) // 2, root.winfo_screenheight() // 4
    root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
    root.update_idletasks()
    make_click_through(root)

    steps_in, steps_out = max(1, FADE_IN_MS // STEP_MS), max(1, FADE_OUT_MS // STEP_MS)

    def fade_in(i=0):
        root.attributes("-alpha", min(0.96, 0.96 * (i + 1) / steps_in))
        root.after(STEP_MS, fade_in, i + 1) if i + 1 < steps_in else root.after(HOLD_MS, fade_out)

    def fade_out(i=0):
        root.attributes("-alpha", max(0.0, 0.96 * (1 - (i + 1) / steps_out)))
        root.after(STEP_MS, fade_out, i + 1) if i + 1 < steps_out else root.destroy()

    fade_in()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
