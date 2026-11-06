"""Build the Patchy desktop app with PyInstaller.

    python -m pip install -r requirements-build.txt
    python build.py

Output goes to release/:
  Windows  Patchy-<version>-windows-x64.exe      (one file)
  macOS    Patchy-<version>-macos-<arch>.zip      (Patchy.app)
  Linux    Patchy-<version>-linux-<arch>.tar.gz   (folder with the Patchy program)
"""
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from patchy import __version__  # noqa: E402

PLAT = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
ARCH = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower(),
                                                                                   platform.machine().lower())
NAME = f"Patchy-{__version__}-{PLAT}-{ARCH}"
DIST, OUT = os.path.join(ROOT, "dist"), os.path.join(ROOT, "release")


def pyinstaller():
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", "Patchy", "--windowed",
            "--icon", os.path.join("patchy", "static", "icon-512.png"),
            "--add-data", f"patchy{os.sep}static{os.pathsep}patchy/static",
            "--collect-all", "netaudio",          # includes its native netaudio_core library
            "--collect-submodules", "zeroconf",
            "--hidden-import", "patchy.gui"]
    if PLAT == "windows":
        args.append("--onefile")
    if PLAT == "macos":
        args += ["--osx-bundle-identifier", "app.patchy.desktop"]
    subprocess.check_call(args + ["run.py"], cwd=ROOT)


def mac_plist(app):
    """Explain the Local Network prompt (macOS 15+), then re-sign since the bundle changed."""
    p = os.path.join(app, "Contents", "Info.plist")
    with open(p, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSLocalNetworkUsageDescription": "Patchy finds Dante devices and Blackmagic Videohubs on your network "
                                          "so it can show their routing. It only reads; it never changes anything.",
        "NSBonjourServices": ["_netaudio-arc._udp", "_netaudio-cmc._udp", "_netaudio-dbc._udp",
                              "_netaudio-chan._udp"],
    })
    with open(p, "wb") as f:
        plistlib.dump(info, f)
    subprocess.check_call(["codesign", "--force", "--deep", "--sign", "-", app])


def package():
    os.makedirs(OUT, exist_ok=True)
    if PLAT == "windows":
        dst = os.path.join(OUT, NAME + ".exe")
        shutil.copy2(os.path.join(DIST, "Patchy.exe"), dst)
        return dst, [dst]
    if PLAT == "macos":
        app = os.path.join(DIST, "Patchy.app")
        mac_plist(app)
        dst = os.path.join(OUT, NAME + ".zip")
        subprocess.check_call(["ditto", "-c", "-k", "--keepParent", app, dst])
        return dst, [os.path.join(app, "Contents", "MacOS", "Patchy")]
    dst = os.path.join(OUT, NAME + ".tar.gz")
    with tarfile.open(dst, "w:gz") as t:
        t.add(os.path.join(DIST, "Patchy"), arcname="Patchy")
    return dst, [os.path.join(DIST, "Patchy", "Patchy")]


def smoke(exe):
    """The packaged app must be able to run netaudio (the Dante reader) as a child process."""
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "out.txt")
    subprocess.run([exe, "--netaudio-child", out, "--help"], timeout=120, check=False)
    txt = open(out, encoding="utf-8", errors="replace").read() if os.path.exists(out) else ""
    err = open(out + ".err", encoding="utf-8", errors="replace").read() if os.path.exists(out + ".err") else ""
    if "Usage" not in txt:
        raise SystemExit(f"Smoke test failed: netaudio didn't run inside the app.\n{txt[-800:]}\n{err[-800:]}")
    # Loads netaudio's native core library too; no Dante devices on a build machine, so any answer will do.
    subprocess.run([exe, "--netaudio-child", out, "--json", "--timeout", "2", "device", "list"], timeout=120, check=False)
    err = open(out + ".err", encoding="utf-8", errors="replace").read() if os.path.exists(out + ".err") else ""
    if "Traceback" in err or "netaudio_core" in err:
        raise SystemExit(f"Smoke test failed: netaudio's core library didn't load.\n{err[-1500:]}")
    print("Smoke test passed")


if __name__ == "__main__":
    pyinstaller()
    artifact, exes = package()
    smoke(exes[0])
    print("Built", artifact)
