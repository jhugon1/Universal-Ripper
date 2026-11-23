"""Start with Traktor: the watcher's decisions, the process helper, the background Ripper's quit rule, and the Startup shortcut script."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import run
import traktor_process
from traktor_watcher import Watcher

ROOT = Path(__file__).resolve().parent.parent


class World:
    """Stand-ins for the process list, the Ripper's port and the launcher."""
    def __init__(self):
        self.pids, self.ripper, self.started = set(), False, 0

    def watcher(self):
        def start():
            self.started += 1
            self.ripper = True
        return Watcher(lambda: self.pids, lambda: self.ripper, start, say=lambda text: None)


def test_ripper_starts_once_when_traktor_opens():
    w = World()
    watcher = w.watcher()
    assert watcher.poll() == "idle" and w.started == 0           # no Traktor: nothing
    w.pids = {100}
    assert watcher.poll() == "started" and w.started == 1         # Traktor opens: the Ripper starts
    assert watcher.poll() == "waiting" and w.started == 1         # ... and not again while Traktor stays open


def test_quitting_the_ripper_while_traktor_is_open_keeps_it_quit():
    w = World()
    watcher = w.watcher()
    w.pids = {100}
    watcher.poll()
    w.ripper = False                                              # the user closed it
    for _ in range(5):
        assert watcher.poll() == "waiting"
    assert w.started == 1


def test_a_new_traktor_session_starts_it_again():
    w = World()
    watcher = w.watcher()
    w.pids = {100}
    watcher.poll()
    w.pids, w.ripper = set(), False                               # Traktor closed
    assert watcher.poll() == "idle"
    w.pids = {200}                                                # Traktor opened again
    assert watcher.poll() == "started" and w.started == 2


def test_a_ripper_the_user_started_is_left_alone():
    w = World()
    w.ripper = True
    watcher = w.watcher()
    w.pids = {100}
    assert watcher.poll() == "already-running" and w.started == 0
    assert watcher.poll() == "waiting"


def test_only_traktor_pro_counts_as_traktor(monkeypatch):
    monkeypatch.delenv("RIPPER_TRAKTOR_PROCESS", raising=False)
    assert traktor_process.is_traktor_name("Traktor Pro 4.exe") and traktor_process.is_traktor_name("Traktor Pro 3.exe")
    for other in ("Traktor Kontrol S4 Driver.exe", "TraktorService.exe", "Traktor Pro 4.dll", "python.exe", ""):
        assert not traktor_process.is_traktor_name(other)
    monkeypatch.setenv("RIPPER_TRAKTOR_PROCESS", "standin")
    assert traktor_process.is_traktor_name("StandIn.exe") and not traktor_process.is_traktor_name("Traktor Pro 4.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process list")
def test_the_process_list_sees_this_process():
    assert os.getpid() in {pid for pid, _ in traktor_process.process_list()}


def test_background_ripper_quits_only_after_traktor_is_gone_and_downloads_are_done():
    assert run.should_quit(3, busy=False, busy_for=0) is False                       # a quick Traktor restart is not a quit
    assert run.should_quit(12, busy=False, busy_for=0) is True
    assert run.should_quit(12, busy=True, busy_for=60) is False                      # a download is running: wait
    assert run.should_quit(12, busy=True, busy_for=run.BUSY_LIMIT_SECONDS) is True   # ... but not forever


def powershell_available():
    return sys.platform == "win32" and shutil.which("powershell") is not None and (ROOT / ".venv" / "Scripts" / "pythonw.exe").is_file()


@pytest.mark.skipif(not powershell_available(), reason="needs Windows PowerShell and the .venv")
def test_the_startup_shortcut_is_made_and_removed(tmp_path):
    def ps(*args):
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "autostart.ps1"),
                               "-StartupDir", str(tmp_path), "-NoStart", *args], capture_output=True, text=True, timeout=60)
    done = ps()
    link = tmp_path / "Ripped Ripper (starts with Traktor).lnk"
    assert done.returncode == 0 and link.is_file(), done.stdout + done.stderr
    shown = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); $s.TargetPath; $s.Arguments"],
                           capture_output=True, text=True).stdout.split("\n")
    assert shown[0].strip().lower().endswith("pythonw.exe") and "traktor_watcher.py" in shown[1]
    assert ps().returncode == 0 and link.is_file()                    # again: still one shortcut
    gone = ps("-Remove")
    assert gone.returncode == 0 and not link.exists()
