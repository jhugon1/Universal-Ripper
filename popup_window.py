"""The "RIPPED" pop-up shown over Traktor: a small borderless always-on-top window that fades in, waits and fades out.

Run by ripper_queue/popup.py as its own process (never imported by the app):
    python popup_window.py --title "Artist - Title" [--lead Already] [--sub "Added to tonight's queue"]
It does not take focus and mouse clicks go through it, so it never gets in the way of the DJ. It is placed over the
Traktor window when one is found, otherwise in the middle of the screen. Windows only (elsewhere it just exits).
"""
import argparse
import ctypes
import ctypes.wintypes
import os
import sys
import tkinter

HOLD_MS = 2300
FADE_IN_MS = 160
FADE_OUT_MS = 650
STEP_MS = 16
BG = "#040201"   # the wordmark picture's own background, so it blends in
EDGE = "#5A4512"
HERE = os.path.dirname(os.path.abspath(__file__))
TOP_OFFSET = 46    # from the top of Traktor's window: the header strip, then the master-clock box (AUTO / MASTER / QUANT / BOOST ...)
POPUP_HEIGHT = 126 # 46..172: covers the box (it spans about 86..168) and stops above the mixer knobs (they start near 176)
MIN_WIDTH = 276    # the box is about 248 wide and centred on Traktor's window: the pop-up always covers all of it
COVER = 100        # album cover (or the Ripper face) on the left


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
    big = [r for r in found if r[2] >= 800 and r[3] >= 500]   # Traktor also keeps small hidden windows for its controller screens
    return max(big, key=lambda r: r[2] * r[3]) if big else None


def make_click_through(window):
    """Layered, transparent to the mouse, never activated, hidden from alt-tab."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    GWL_EXSTYLE, LAYERED, TRANSPARENT, TOOLWINDOW, NOACTIVATE = -20, 0x80000, 0x20, 0x80, 0x8000000
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE)


def load_cover(source, size):
    """A square cover picture: from an image file, or the cover embedded in an audio file; the Ripper face when there is none."""
    import io

    from PIL import Image
    img = None
    if source and os.path.isfile(source):
        try:
            if source.lower().endswith((".jpg", ".jpeg", ".png")):
                img = Image.open(source)
            else:
                from mutagen.id3 import ID3
                frames = ID3(source).getall("APIC")
                img = Image.open(io.BytesIO(frames[0].data)) if frames else None
        except Exception:
            img = None
    if img is None:
        img = Image.open(os.path.join(HERE, "static", "ripped_face.jpg"))
    img = img.convert("RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def ellipsize(text, limit=48):
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--lead", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--cover", default="", help="an audio file with an embedded cover, or an image file; the Ripper face is used when there is none")
    args = ap.parse_args(argv)
    if sys.platform != "win32":
        return 0
    from PIL import Image, ImageTk

    root = tkinter.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.0)
    root.configure(bg=EDGE)
    inner = tkinter.Frame(root, bg=BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)

    cover = load_cover(args.cover, COVER)
    cover_photo = ImageTk.PhotoImage(cover)
    tkinter.Label(inner, image=cover_photo, bg=BG, bd=0).pack(side="left", padx=(12, 4), pady=12)

    text = tkinter.Frame(inner, bg=BG)
    text.pack(side="left", fill="both", expand=True, padx=(8, 16))
    tkinter.Frame(text, bg=BG).pack(expand=True)                                  # centres the text block vertically
    head = tkinter.Frame(text, bg=BG)
    head.pack(anchor="w")
    if args.lead:
        tkinter.Label(head, text=args.lead, bg=BG, fg="#ECEDEE", font=("Segoe UI Semibold", 20)).pack(side="left", padx=(0, 10))
    mark = Image.open(os.path.join(HERE, "static", "ripped_wordmark.png")).convert("RGBA")
    height = 36
    mark = mark.resize((round(mark.width * height / mark.height), height), Image.LANCZOS)
    photo = ImageTk.PhotoImage(mark)
    tkinter.Label(head, image=photo, bg=BG).pack(side="left")
    tkinter.Label(text, text=ellipsize(args.title, 40), bg=BG, fg="#ECEDEE", font=("Segoe UI Semibold", 13), anchor="w").pack(fill="x", pady=(2, 0))
    if args.sub:
        tkinter.Label(text, text=ellipsize(args.sub, 46), bg=BG, fg="#9BA0A8", font=("Segoe UI", 10), anchor="w").pack(fill="x")
    tkinter.Frame(text, bg=BG).pack(expand=True)

    root.update_idletasks()
    width, height_ = max(MIN_WIDTH, root.winfo_reqwidth() + 8), POPUP_HEIGHT   # as wide as the text needs, never narrower than the box it must cover
    rect = traktor_rect()
    if rect:
        left, top, rw, rh = rect
        x, y = left + (rw - width) // 2, top + TOP_OFFSET
    else:
        x, y = (root.winfo_screenwidth() - width) // 2, root.winfo_screenheight() // 4
    root.geometry(f"{width}x{height_}+{x}+{y}")
    root.update_idletasks()
    make_click_through(root)

    steps_in, steps_out = max(1, FADE_IN_MS // STEP_MS), max(1, FADE_OUT_MS // STEP_MS)

    def fade_in(i=0):
        root.attributes("-alpha", min(1.0, (i + 1) / steps_in))
        root.after(STEP_MS, fade_in, i + 1) if i + 1 < steps_in else root.after(HOLD_MS, fade_out)

    def fade_out(i=0):
        root.attributes("-alpha", max(0.0, 1 - (i + 1) / steps_out))
        root.after(STEP_MS, fade_out, i + 1) if i + 1 < steps_out else root.destroy()

    fade_in()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
