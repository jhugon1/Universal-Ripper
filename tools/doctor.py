#!/usr/bin/env python3
"""tools/doctor.py - READ-ONLY check of your setup + a map of the current code. Changes nothing.
Run from the repo:  python tools/doctor.py [--offline]"""
import argparse, ast, os, re, shutil, socket, sqlite3, subprocess, sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("USERPROFILE") or Path.home())
AUDIO = {".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif"}
BAD = [0]


def head(t): print(f"\n== {t} ==")
def ok(m): print(f"  [ok] {m}")
def info(m): print(f"  [--] {m}")
def warn(m): BAD[0] += 1; print(f"  [!!] {m}")


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def check_env():
    head("PYTHON / DEPENDENCIES")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    (ok if in_venv else warn)(f"python {sys.version.split()[0]} | {sys.executable} | venv={in_venv}")
    for dist in ["fastapi", "uvicorn", "yt-dlp", "ytmusicapi", "jinja2", "python-multipart", "PyQt6", "spotdl", "mutagen"]:
        try:
            ok(f"{dist} {version(dist)}")
        except PackageNotFoundError:
            (info if dist == "mutagen" else warn)(f"{dist} not installed" + (" (optional, used for ID3 tags/duration)" if dist == "mutagen" else ""))
    for tool in ["ffmpeg", "ffprobe", "git"]:
        p = shutil.which(tool)
        if not p:
            warn(f"{tool} not on PATH"); continue
        try:
            flag = "--version" if tool == "git" else "-version"
            first = subprocess.run([p, flag], capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        except Exception as e:
            first = f"(could not run: {e})"
        ok(f"{tool}: {first[:70]}")


def newest_traktor_dir(ni):
    dirs = [d for d in ni.glob("Traktor*") if d.is_dir()]
    return max(dirs, key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)] or [0]) if dirs else None


def check_traktor():
    head("TRAKTOR (user data in Documents)")
    ni = next((p for p in [HOME / "Documents" / "Native Instruments", HOME / "OneDrive" / "Documents" / "Native Instruments"] if p.is_dir()), None)
    if not ni:
        warn("no 'Documents\\Native Instruments' folder found"); return
    ok(f"found {ni}")
    t = newest_traktor_dir(ni)
    if not t:
        warn("no Traktor <version> folder inside it"); return
    ok(f"newest Traktor data folder: {t.name}")
    nml = t / "collection.nml"
    if nml.exists():
        n = nml.read_bytes().count(b"<ENTRY ")
        ok(f"collection.nml: {nml.stat().st_size/1e6:.1f} MB, ~{n} track entries")
    else:
        warn("collection.nml missing")
    hist = t / "History"
    files = sorted(hist.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:3] if hist.is_dir() else []
    ok(f"History: newest = {', '.join(f.name for f in files)}") if files else info("History folder empty/missing (fills after you play tracks)")

    head("TRAKTOR (program files / QML mod target)")
    roots = [Path(os.environ.get(k, "")) / "Native Instruments" for k in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(k)]
    found = False
    for r in roots:
        for csi in r.glob("Traktor*/**/qml/CSI") if r.is_dir() else []:
            found = True
            ok(f"CSI folder: {csi}")
            kids = sorted(p.name for p in csi.iterdir() if p.is_dir())
            info("controller mappings: " + ", ".join(kids))
            d2 = csi / "D2"
            if d2.is_dir():
                ok(f"D2 mapping present; D2.qml mentions ApiModule: {'yes' if 'ApiModule' in (d2/'D2.qml').read_text(errors='ignore') else 'no (mod not installed)'}; Api folder: {(d2/'Api').is_dir()}")
                info(f"this terminal is elevated (admin): {is_admin()}  (editing files under Program Files needs an Administrator terminal)")
            else:
                warn("no D2 folder in this Traktor version -> the mod's install approach needs rethinking")
    if not found:
        warn("could not find <Program Files>\\Native Instruments\\Traktor*\\...\\qml\\CSI")

    head("MUSIC FOLDERS")
    for p in [HOME / "Music" / "Traktor", HOME / "Music" / "iTunes", HOME / "Music" / "Traktor" / "Ripper Queue"]:
        if p.is_dir():
            n = sum(1 for f in p.rglob("*") if f.suffix.lower() in AUDIO)
            ok(f"{p}  ({n} audio files)")
        else:
            info(f"{p}  (does not exist yet)")


def check_ports():
    head("PORTS")
    for port, why in [(8000, "our FastAPI app"), (8080, "Traktor mod -> receiver")]:
        s = socket.socket()
        if os.name != "nt":  # on Windows SO_REUSEADDR would let bind succeed on a port that is in use
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port)); ok(f"{port} free ({why})")
        except OSError:
            warn(f"{port} already in use ({why})")
        finally:
            s.close()


def check_network():
    head("NETWORK (YouTube Music search)")
    try:
        from ytmusicapi import YTMusic
        r = YTMusic().search("daft punk one more time", filter="songs", limit=1)
        ok(f"search works -> {', '.join(a['name'] for a in r[0]['artists'])} - {r[0]['title']}")
    except Exception as e:
        warn(f"search failed: {type(e).__name__}: {str(e)[:100]}")


def code_map():
    head("WEB APP ROUTES (app.py, parsed - not imported)")
    tree = ast.parse((REPO / "app.py").read_text(encoding="utf-8"))
    blockers = []
    for n in sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), key=lambda n: n.lineno):
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and isinstance(d.func.value, ast.Name) and d.func.value.id == "app":
                path = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else "?"
                print(f"  {d.func.attr.upper():5} {path:22} {n.name}  (line {n.lineno})")
                if isinstance(n, ast.AsyncFunctionDef):
                    calls = {(c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, 'id', '')) for c in ast.walk(n) if isinstance(c, ast.Call)}
                    hit = calls & {"search", "connect", "YTMusic", "run"}
                    if hit:
                        blockers.append(f"{n.name}: {', '.join(sorted(hit))}")
    if blockers:
        warn("async handlers doing blocking work (stalls the event loop under load):")
        for b in blockers: print(f"         - {b}")

    head("DESKTOP GUI (ripper_gui.py)")
    tree = ast.parse((REPO / "ripper_gui.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            print(f"  class {n.name:22} {sum(isinstance(m, ast.FunctionDef) for m in n.body):2} methods  (line {n.lineno})")

    head("DATABASE (queue.db, opened read-only)")
    db = REPO / "queue.db"
    if db.exists():
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            for name, sql in c.execute("select name, sql from sqlite_master where type='table'"):
                print(f"  {name}: {c.execute(f'select count(*) from {name}').fetchone()[0]} rows")
                print("   ", " ".join(sql.split())[:160])
        except Exception as e:
            warn(f"could not read queue.db: {e}")
    else:
        info("no queue.db yet")

    head("TRACKED FILES THAT SHOULD NOT BE IN GIT")
    try:
        names = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
        bad = [(n, (REPO / n).stat().st_size) for n in names if (REPO / n).exists() and (Path(n).suffix.lower() in AUDIO or n.endswith(".db"))]
        for n, s in bad: print(f"  {s/1e6:6.1f} MB  {n}")
        warn(f"{len(bad)} audio/db files are tracked ({sum(s for _, s in bad)/1e6:.1f} MB) - .gitignore only covers untracked files") if bad else ok("none")
    except Exception as e:
        info(f"git check skipped: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--offline", action="store_true"); a = ap.parse_args()
    print(f"repo: {REPO}\nhome: {HOME}")
    check_env(); check_traktor(); check_ports()
    if not a.offline: check_network()
    code_map()
    print(f"\n== DONE: {BAD[0]} item(s) marked [!!] ==")