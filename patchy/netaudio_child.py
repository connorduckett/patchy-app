"""Runs the netaudio CLI in a child process and writes its output to a file.

Patchy calls this as `<patchy> --netaudio-child OUTFILE <netaudio args...>` so the same thing
works from source and from a packaged app (which has no separate python.exe). Output goes to a
file rather than a pipe because a windowed app on Windows has no usable stdout.
"""
import sys


def run(outfile, args):
    out = open(outfile, "w", encoding="utf-8")
    err = open(outfile + ".err", "w", encoding="utf-8")
    sys.stdout, sys.stderr = out, err
    code = 0
    try:
        sys.argv = ["netaudio", *args]
        from netaudio.cli import main
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except ImportError as e:
        err.write(f"netaudio isn't installed ({e}). Run: pip install netaudio")
        code = 3
    except Exception as e:  # noqa: BLE001
        err.write(f"{type(e).__name__}: {e}")
        code = 1
    finally:
        out.flush()
        err.flush()
    return code
