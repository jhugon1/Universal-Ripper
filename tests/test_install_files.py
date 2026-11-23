"""Guards for the easy-install files: what a fresh machine needs is what requirements.txt installs, the Windows scripts stay readable
by Windows PowerShell 5.1, and the `--check` helpers answer sensibly."""
import ast
import re
import sys
from pathlib import Path

import pytest

import run

ROOT = Path(__file__).resolve().parent.parent
# import name -> the requirements.txt entry that provides it
PROVIDED_BY = {"PIL": "pillow", "fastapi": "fastapi", "starlette": "fastapi", "uvicorn": "uvicorn", "yt_dlp": "yt-dlp",
               "ytmusicapi": "ytmusicapi", "mutagen": "mutagen", "jinja2": "jinja2", "multipart": "python-multipart"}
DEV_ONLY = {"watchfiles", "pytest", "httpx"}   # requirements-dev.txt


def requirement_names():
    return {re.split(r"[<>=\[ ;]", line.strip(), 1)[0].lower() for line in (ROOT / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")}


def third_party_imports():
    local = {p.stem for p in ROOT.glob("*.py")} | {p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    found = {}
    files = list(ROOT.glob("*.py")) + list((ROOT / "ripper_queue").glob("*.py")) + list((ROOT / "traktor").glob("*.py"))
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                [node.module.split(".")[0]] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for m in mods:
                if m not in sys.stdlib_module_names and m not in local and m not in DEV_ONLY:
                    found.setdefault(m, set()).add(f.name)
    return found


def test_requirements_cover_every_import_the_app_makes():
    have = requirement_names()
    for module, files in third_party_imports().items():
        assert module in PROVIDED_BY, f"{module} (imported by {sorted(files)}) is not known: add it to requirements.txt and PROVIDED_BY"
        assert PROVIDED_BY[module] in have, f"{module} needs '{PROVIDED_BY[module]}' in requirements.txt"
    assert "pillow" in have   # the pop-up, the button and the panel all draw with it


@pytest.mark.parametrize("path", ["Setup.bat", "Start Ripper.bat", "setup.ps1", "autostart.ps1", "Turn on auto-start.bat",
                                  "Turn off auto-start.bat", "traktor_mod/install_traktor_mod.ps1",
                                  "traktor_mod/Install Traktor Mod.bat", "traktor_mod/Remove Traktor Mod.bat"])
def test_windows_scripts_are_ascii_with_crlf(path):
    raw = (ROOT / path).read_bytes()
    raw.decode("ascii")                       # Windows PowerShell 5.1 reads a script without a BOM as ANSI: no fancy characters
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{path} must use CRLF line endings"
    attrs = (ROOT / ".gitattributes").read_text()
    assert "*.bat text eol=crlf" in attrs and "*.ps1 text eol=crlf" in attrs   # and a clone must not change that


def test_setup_passes_no_double_quotes_inside_python_snippets():
    """Windows PowerShell 5.1 drops embedded double quotes from arguments handed to python.exe, which broke `python -c "..."`."""
    for line in (ROOT / "setup.ps1").read_text(encoding="ascii").splitlines():
        if re.search(r"&\s+\$\w+(\s+@\w+)?\s+-c\s", line) and "$code" not in line:
            snippet = line.split(" -c ", 1)[1]
            if snippet.startswith("'"):
                assert '"' not in snippet.split("'")[1], line


def test_the_check_report_is_sensible(monkeypatch):
    rows = run.environment_report(port=1)     # nothing listens on port 1
    texts = " ".join(t for _, _, t in rows)
    assert "Python" in texts and "packages" in texts and "ffmpeg" in texts
    assert all(isinstance(ok, bool) and isinstance(req, bool) for ok, req, _ in rows)
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    missing = [t for ok, req, t in run.environment_report(port=1) if req and not ok]
    assert any("ffmpeg not found" in t for t in missing) and any("ffprobe not found" in t for t in missing)


def test_app_is_running_is_false_for_an_unused_port():
    assert run.app_is_running(port=1) is False


def test_traktor_mod_detection(tmp_path):
    d2 = tmp_path / "Resources64" / "qml" / "CSI" / "D2"
    d2.mkdir(parents=True)
    (d2 / "D2.qml").write_text("Mapping\n{\n}\n")
    assert run.traktor_mod_installed(tmp_path) is False
    (d2 / "D2.qml").write_text("Mapping\n{\n  ApiModule {}\n}\n")
    assert run.traktor_mod_installed(tmp_path) is True
    assert run.traktor_mod_installed(tmp_path / "nowhere") is False
