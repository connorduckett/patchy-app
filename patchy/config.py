"""Settings, kept in settings.json in the data folder. Most can be changed from the web UI."""
import json
import os
import re
import secrets

DEFAULTS = {
    "site_name": "",              # shown next to the logo
    "port": 8760,
    "allow_lan": False,           # False: only this computer can open Patchy
    "bind": None,                 # advanced: overrides allow_lan
    "token": "",                  # edit token for other devices (generated)
    "trust_localhost": True,      # edits made on this computer don't need the token
    "open_browser": True,
    "dante_enabled": True,
    "sync_minutes": 5,
    "netaudio_timeout": 8,
    "netaudio_interface": None,
    "auto_baseline_syncs": 3,
    "room_rules": [],             # [[regex on the Dante name, room name], ...]
    "videohubs": [],              # [{"host", "name", "room"}, ...]
    "videohub_seconds": 60,
}
EDITABLE = ("site_name", "port", "allow_lan", "dante_enabled", "sync_minutes", "netaudio_timeout",
            "netaudio_interface", "room_rules", "videohubs", "open_browser")
NEEDS_RESTART = ("port", "allow_lan")


def path(data_dir):
    return os.path.join(data_dir, "settings.json")


def load(data_dir):
    cfg = dict(DEFAULTS)
    p = path(data_dir)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            cfg.update(json.load(f))
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(18)
        save(data_dir, cfg)
    cfg["data_dir"] = data_dir
    return cfg


def save(data_dir, cfg):
    keep = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    tmp = path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)
    os.replace(tmp, path(data_dir))


def bind_address(cfg):
    return cfg.get("bind") or ("0.0.0.0" if cfg.get("allow_lan") else "127.0.0.1")


def clean(changes):
    """Validate settings sent from the UI; raises ValueError with a readable message."""
    out = {}
    for k, v in changes.items():
        if k not in EDITABLE:
            continue
        if k == "site_name":
            v = str(v or "").strip()[:60]
        elif k == "port":
            v = int(v)
            if not 1024 <= v <= 65535:
                raise ValueError("Port must be between 1024 and 65535")
        elif k in ("allow_lan", "dante_enabled", "open_browser"):
            v = bool(v)
        elif k == "sync_minutes":
            v = max(1, min(int(v), 1440))
        elif k == "netaudio_timeout":
            v = max(2, min(int(v), 60))
        elif k == "netaudio_interface":
            v = str(v or "").strip() or None
        elif k == "room_rules":
            rules = []
            for pattern, room in v:
                pattern, room = str(pattern).strip(), str(room).strip()
                if not pattern or not room:
                    continue
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ValueError(f"Room rule '{pattern}' isn't a valid pattern: {e}") from None
                rules.append([pattern, room])
            v = rules
        elif k == "videohubs":
            hubs = []
            for h in v:
                host = str(h.get("host", "")).strip()
                if not host:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9.\-:]+", host):
                    raise ValueError(f"'{host}' isn't an IP address or host name")
                hubs.append({"host": host, "name": str(h.get("name", "")).strip(), "room": str(h.get("room", "")).strip()})
            v = hubs
        out[k] = v
    return out
