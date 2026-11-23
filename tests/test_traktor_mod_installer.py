"""The Traktor mod installer (traktor_mod/install_traktor_mod.ps1), run for real against a made-up Traktor folder in a temp dir.
Traktor's own D2.qml is not in the repo, so the test writes a small stand-in with the same two anchor spots."""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "traktor_mod" / "install_traktor_mod.ps1"
POWERSHELL = shutil.which("powershell")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not POWERSHELL, reason="Windows PowerShell only")

D2 = ('\nimport CSI 1.0\nimport QtQuick 2.0\n\nimport "../../Defines"\nimport "../Common"\nimport "../Common/Settings"\n\n'
      'Mapping\n{\n  Something {}\n}\n')


def run(traktor, home, *args):
    env = dict(os.environ, USERPROFILE=str(home))
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-TraktorDir", str(traktor),
                           "-NoElevate", "-NoPause", *args], capture_output=True, text=True, env=env, timeout=120)


def make_traktor(tmp_path, eol="\n"):
    d2dir = tmp_path / "Traktor" / "Resources64" / "qml" / "CSI" / "D2"
    d2dir.mkdir(parents=True)
    original = D2.replace("\n", eol)
    (d2dir / "D2.qml").write_bytes(original.encode("ascii"))
    return tmp_path / "Traktor", d2dir, original


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_install_is_exact_idempotent_and_removable(tmp_path, eol):
    traktor, d2dir, original = make_traktor(tmp_path, eol)
    home = tmp_path / "home"
    home.mkdir()

    done = run(traktor, home)
    assert done.returncode == 0, done.stdout + done.stderr
    text = (d2dir / "D2.qml").read_bytes().decode("ascii")
    expected = original.replace('import "../Common/Settings"' + eol, 'import "../Common/Settings"' + eol + 'import "./Api"' + eol, 1)
    expected = expected.replace("Mapping" + eol + "{" + eol, "Mapping" + eol + "{" + eol + "  ApiModule {}" + eol + eol, 1)
    assert text == expected                                     # the two lines, in the right places, with the file's own line endings
    assert (home / "ripper-traktor-backup" / "D2.qml.orig").read_bytes().decode("ascii") == original
    for api in (ROOT / "traktor_mod" / "Api").iterdir():       # the bundled files arrive byte for byte
        assert hashlib.sha256((d2dir / "Api" / api.name).read_bytes()).hexdigest() == hashlib.sha256(api.read_bytes()).hexdigest()

    again = run(traktor, home)                                  # a second run changes nothing
    assert again.returncode == 0 and (d2dir / "D2.qml").read_bytes().decode("ascii") == expected

    gone = run(traktor, home, "-Uninstall")
    assert gone.returncode == 0, gone.stdout + gone.stderr
    assert (d2dir / "D2.qml").read_bytes().decode("ascii") == original   # exactly the original again
    assert not (d2dir / "Api").exists()


def test_a_different_traktor_changes_nothing(tmp_path):
    traktor, d2dir, _ = make_traktor(tmp_path)
    odd = "import CSI 1.0\nSomethingElse\n{\n}\n"
    (d2dir / "D2.qml").write_text(odd, encoding="ascii")
    done = run(traktor, tmp_path)
    assert done.returncode == 1 and "Nothing was changed" in done.stdout
    assert (d2dir / "D2.qml").read_text(encoding="ascii") == odd and not (d2dir / "Api").exists()


def test_the_bundled_files_match_the_pinned_hashes():
    text = SCRIPT.read_text(encoding="ascii")
    for api in (ROOT / "traktor_mod" / "Api").iterdir():
        assert f"'{api.name}' = '{hashlib.sha256(api.read_bytes()).hexdigest()}'" in text
