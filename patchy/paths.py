"""Where Patchy keeps its data (database, settings, log)."""
import os
import sys

APP = "Patchy"


def frozen():
    return bool(getattr(sys, "frozen", False))


def app_dir():
    """Folder holding the app itself (the exe when packaged, the repo when run from source)."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir(override=None):
    """--data / PATCHY_DATA, then a "patchy-data" folder next to the app (portable mode),
    then the normal per-user place for this OS."""
    d = override or os.environ.get("PATCHY_DATA")
    if not d:
        portable = os.path.join(app_dir(), "patchy-data")
        if os.path.isdir(portable):
            d = portable
        elif sys.platform == "win32":
            d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP)
        elif sys.platform == "darwin":
            d = os.path.expanduser("~/Library/Application Support/" + APP)
        else:
            d = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "patchy")
    os.makedirs(d, exist_ok=True)
    return d


def static_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
