"""Start Patchy when the user logs in (per user, no admin rights)."""
import os
import plistlib
import shlex
import subprocess
import sys

from . import paths

NAME = "Patchy"
MAC_ID = "app.patchy.launcher"


def command():
    if paths.frozen():
        return [sys.executable, "--minimized"]
    exe = sys.executable
    if sys.platform == "win32" and os.path.basename(exe).lower() == "python.exe":
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")   # no console window
    return [exe, os.path.join(paths.app_dir(), "run.py"), "--minimized"]


def _mac_plist():
    return os.path.expanduser(f"~/Library/LaunchAgents/{MAC_ID}.plist")


def _linux_desktop():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "patchy.desktop")


def is_enabled():
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, NAME)
            return True
        if sys.platform == "darwin":
            return os.path.exists(_mac_plist())
        return os.path.exists(_linux_desktop())
    except OSError:
        return False


def set_enabled(on):
    cmd = command()
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        p = _mac_plist()
        if on:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                plistlib.dump({"Label": MAC_ID, "ProgramArguments": cmd, "RunAtLoad": True}, f)
        elif os.path.exists(p):
            os.remove(p)
    else:
        p = _linux_desktop()
        if on:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Patchy\n"
                        f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\n")
        elif os.path.exists(p):
            os.remove(p)
