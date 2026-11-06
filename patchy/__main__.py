"""Start Patchy.

  patchy                 open the Patchy window and the map in your browser
  patchy --headless      no window (for a server or a computer without a desktop)
  patchy --minimized     start minimized without opening the browser (used at login)
  patchy --no-browser    don't open the browser
  patchy --data DIR      keep the database and settings in DIR
  patchy sync            read Dante once, then exit
  patchy videohubs       read the Videohubs once, then exit
"""
import argparse
import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser

# A windowed app has no console: give print/argparse somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from . import __version__, config, dante, db, paths, videohub, web  # noqa: E402


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "patchy"
    except Exception:  # noqa: BLE001
        return False


def open_folder(path):
    import subprocess
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--netaudio-child"]:
        from .netaudio_child import run
        sys.exit(run(argv[1], argv[2:]))

    ap = argparse.ArgumentParser(prog="patchy", description="A live map of your AV system's signal flow.")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "sync", "videohubs"])
    ap.add_argument("--data", help="folder for the database and settings")
    ap.add_argument("--headless", action="store_true", help="no window")
    ap.add_argument("--minimized", action="store_true", help="start minimized, don't open the browser")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--version", action="version", version=f"Patchy {__version__}")
    args, _unknown = ap.parse_known_args(argv)   # macOS may add arguments of its own

    data = paths.data_dir(args.data)
    logging.basicConfig(filename=os.path.join(data, "patchy.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(data)
    store = db.open_store(data)
    sync = dante.DanteSync(store, cfg)
    vh = videohub.VideohubSync(store, cfg)

    if args.command == "sync":
        sync.sync("cli")
        print(store.one("SELECT * FROM syncs ORDER BY id DESC LIMIT 1"))
        return
    if args.command == "videohubs":
        vh.sync_all()
        print(vh.status)
        return

    if already_running(cfg["port"]):   # second launch: just show the one that's running
        if not args.minimized:
            webbrowser.open(f"http://localhost:{cfg['port']}/")
        return

    stop = threading.Event()
    httpd = None
    for port in range(cfg["port"], cfg["port"] + 20):   # port taken by another program: use the next free one
        try:
            httpd = web.make_server(store, sync, cfg, vh, on_quit=stop.set, port=port)
            break
        except OSError:
            logging.warning("port %s is in use", port)
    if httpd is None:
        raise SystemExit(f"No free port between {cfg['port']} and {cfg['port'] + 19}")
    url = f"http://localhost:{port}/"
    logging.info("Patchy %s on %s (data in %s)", __version__, url, data)
    print(f"Patchy {__version__} on {url}  (data: {data})", flush=True)

    sync.start_background()
    vh.start_background()
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    if not (args.minimized or args.no_browser) and cfg.get("open_browser", True):
        webbrowser.open(url)

    try:
        gui = None
        if not args.headless:
            try:
                from . import gui
            except ImportError:   # a Python without tkinter
                gui = None
        if gui:
            gui.run(url, cfg, stop, port, minimized=args.minimized, open_folder=open_folder)
        else:   # no window: run until Ctrl+C or Quit in Settings
            while not stop.wait(1):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        logging.info("Patchy stopped")


if __name__ == "__main__":
    main()
