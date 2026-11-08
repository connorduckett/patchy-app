"""HTTP API and static files (stdlib only)."""
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, autostart, config, library, paths

STATIC = paths.static_dir()
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("text/javascript", ".js")   # Windows registry can map .js to something odd
KINDS = {"analog", "aes", "dante", "madi", "sdi", "hdmi", "dmx", "sacn", "ethernet", "usb", "other"}
CATEGORIES = {"audio", "video", "lighting", "network", "control"}
# Ports can only be patched within a signal family (a DMX output can't feed an SDI input).
# "other" patches to anything except Dante; Dante only patches to Dante.
FAMILY = {"analog": "audio", "aes": "audio", "madi": "audio", "dante": "dante", "sdi": "video", "hdmi": "video",
          "dmx": "lighting", "sacn": "lighting", "ethernet": "network", "usb": "usb", "other": "other"}


def can_patch(kind_a, kind_b):
    fa, fb = FAMILY.get(kind_a, "other"), FAMILY.get(kind_b, "other")
    if "dante" in (fa, fb):
        return fa == fb
    return fa == fb or "other" in (fa, fb)


DEVICE_FIELDS = ("name", "room_id", "category", "make", "model", "ip", "control_ip", "location", "notes", "x", "y")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def lan_address():
    """This computer's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            return sk.getsockname()[0]
    except OSError:
        return ""


def make_handler(store, sync, cfg, vh=None, on_quit=None):
    class H(BaseHTTPRequestHandler):
        server_version = "Patchy/" + __version__

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- plumbing ------------------------------------------------------
        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))

        def _who(self):
            return self.client_address[0]

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _auth(self):
            if cfg.get("trust_localhost", True) and self._local():
                return   # someone sitting at the host PC
            tok = self.headers.get("X-Token", "")
            if not secrets.compare_digest(tok, cfg["token"]):
                raise ApiError(401, "Edit token required")

        def _route(self, method):
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                if method == "GET":
                    return self._get(path, qs)
                self._auth()
                body = self._body()
                return self._write(method, path, body)
            except ApiError as e:
                self._send(e.code, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {"error": f"Bad request: {e}"})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": str(e)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        # -- reads ---------------------------------------------------------
        def _get(self, path, qs):
            if path == "/api/state":
                st = store.state()
                st["sync_running"] = sync.running
                st["sync_minutes"] = cfg.get("sync_minutes", 5)
                st["videohubs"] = vh.status if vh else {}
                st["dante_enabled"] = cfg.get("dante_enabled", True)
                st["site_name"] = cfg.get("site_name", "")
                st["version"] = __version__
                return self._send(200, st)
            if path == "/api/ping":
                return self._send(200, {"app": "patchy", "version": __version__})
            if path == "/api/settings":
                return self._send(200, self._settings())
            if path == "/api/changes":
                return self._send(200, store.q("SELECT * FROM changes ORDER BY id DESC LIMIT 300"))
            if path == "/api/syncs":
                return self._send(200, store.q("SELECT * FROM syncs ORDER BY id DESC LIMIT 50"))
            if path == "/api/export.csv":
                room = (qs.get("room") or [""])[0]
                return self._csv(int(room) if room.isdigit() else None)
            if path == "/api/check-token":
                ok = secrets.compare_digest(self.headers.get("X-Token", ""), cfg["token"])
                return self._send(200 if ok else 401, {"ok": ok})
            return self._static(path)

        def _settings(self):
            local = self._local()
            out = {k: cfg.get(k) for k in config.EDITABLE}
            out.update(version=__version__, local=local, lan_ip=lan_address() if cfg.get("allow_lan") else "",
                       running_port=self.server.server_address[1])
            if local:   # only shown to someone at this computer
                out.update(token=cfg.get("token"), data_dir=cfg.get("data_dir"), autostart=autostart.is_enabled())
            return out

        def _static(self, path):
            if path in ("/", ""):
                path = "/index.html"
            full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        def _csv(self, room_id):
            rows = store.q("""
              SELECT r.name room, d.name device, p.direction, p.kind, p.number, p.name port, p.label,
                     d2.name other_device, p2.name other_port, c.documented, c.live, c.live_label, c.cable, c.notes
              FROM ports p JOIN devices d ON d.id = p.device_id
              LEFT JOIN rooms r ON r.id = d.room_id
              LEFT JOIN connections c ON (p.direction = 'out' AND c.from_port = p.id) OR (p.direction = 'in' AND c.to_port = p.id)
              LEFT JOIN ports p2 ON p2.id = CASE WHEN p.direction = 'out' THEN c.to_port ELSE c.from_port END
              LEFT JOIN devices d2 ON d2.id = p2.device_id
              WHERE d.hidden = 0 AND (? IS NULL OR d.room_id = ?) AND (p.kind <> 'dante' OR c.id IS NOT NULL)
              ORDER BY r.sort, d.name, p.direction DESC, p.kind, COALESCE(p.number, 99999), p.name""",
                           (room_id, room_id))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Room", "Device", "Direction", "Type", "#", "Port", "Label", "Connected device",
                        "Connected port", "Status", "Cable", "Notes"])
            for r in rows:
                if r["other_device"] is None:
                    status = ""
                elif r["kind"] == "dante" or r["live"]:
                    status = ("OK" if r["documented"] else "Undocumented") if r["live"] else "Missing"
                else:
                    status = "Documented"
                w.writerow([r["room"] or "", r["device"], r["direction"], r["kind"], r["number"] or "",
                            r["port"], r["label"], r["other_device"] or "", r["other_port"] or "", status,
                            r["cable"] or "", r["notes"] or ""])
            name = "patchy-" + time.strftime("%Y-%m-%d") + ".csv"
            self._send(200, buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})

        # -- writes --------------------------------------------------------
        def _write(self, method, path, b):
            who = self._who()
            m = re.fullmatch(r"/api/(\w+)(?:/(\d+))?(?:/(\w+))?", path)
            if not m:
                raise ApiError(404, "not found")
            res, rid, action = m.group(1), m.group(2), m.group(3)
            rid = int(rid) if rid else None

            if res in ("settings", "quit") and not self._local():
                raise ApiError(403, "Settings can only be changed on the computer running Patchy")
            if res == "settings" and method == "PATCH":
                if "autostart" in b:
                    autostart.set_enabled(bool(b.pop("autostart")))
                try:
                    changes = config.clean(b)
                except ValueError as e:
                    raise ApiError(400, str(e)) from None
                changes = {k: v for k, v in changes.items() if v != cfg.get(k)}
                restart = any(k in changes for k in config.NEEDS_RESTART)
                cfg.update(changes)
                config.save(cfg["data_dir"], cfg)
                if changes:
                    store.log(who, "Changed settings: " + ", ".join(sorted(changes)))
                return self._send(200, {"ok": True, "restart_needed": restart})
            if res == "quit" and method == "POST":
                self._send(200, {"ok": True})
                if on_quit:
                    threading.Thread(target=on_quit, daemon=True).start()
                return None

            if res == "sync" and method == "POST":
                started = sync.sync_async(who)
                if vh:
                    threading.Thread(target=vh.sync_all, daemon=True).start()
                return self._send(202, {"started": started})

            if res == "xp" and method == "POST":   # accept router crosspoints: documented := live
                ids = [int(i) for i in b["port_ids"]]
                marks = ",".join("?" * len(ids)) or "NULL"
                store.x(f"UPDATE ports SET xp_doc = xp_live WHERE xp_live IS NOT NULL AND id IN ({marks})", tuple(ids))
                store.log(who, f"Accepted {len(ids)} router route(s) as documented")
                return self._send(200, {"ok": True})

            if res == "rooms":
                if method == "POST":
                    name = b["name"].strip()
                    if not name:
                        raise ValueError("name")
                    new = store.room_id(name)
                    store.log(who, f"Added room {name}")
                    return self._send(201, {"id": new})
                if method == "PATCH":
                    store.x("UPDATE rooms SET name = COALESCE(?, name), sort = COALESCE(?, sort) WHERE id = ?",
                            (b.get("name"), b.get("sort"), rid))
                    store.log(who, f"Renamed room {rid} to {b.get('name')}")
                    return self._send(200, {"ok": True})
                if method == "DELETE":
                    store.x("DELETE FROM rooms WHERE id = ?", (rid,))
                    store.log(who, f"Deleted room {rid}")
                    return self._send(200, {"ok": True})

            if res == "devices":
                if method == "POST" and rid is None:
                    if b.get("category", "audio") not in CATEGORIES:
                        raise ValueError("category")
                    new = store.x("INSERT INTO devices(room_id, name, category, make, model, ip, location, notes, x, y) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (b.get("room_id"), b["name"].strip(), b.get("category", "audio"), b.get("make", ""),
                                   b.get("model", ""), b.get("ip", ""), b.get("location", ""), b.get("notes", ""),
                                   b.get("x", 60), b.get("y", 60)))
                    store.log(who, f"Added device {b['name']}")
                    if b.get("library"):
                        library.apply(store, new, b["library"], b.get("choices") or {}, who)
                    return self._send(201, {"id": new})
                dev = store.one("SELECT * FROM devices WHERE id = ?", (rid,))
                if not dev:
                    raise ApiError(404, "no such device")
                if method == "POST" and action == "library":
                    return self._send(200, library.apply(store, rid, b["model"], b.get("choices") or {}, who))
                if method == "PATCH" and "dante_name" in b and (b["dante_name"] or None) != dev["dante_name"]:
                    library.link_dante(store, rid, b["dante_name"], who)
                if method == "PATCH":
                    fields = {k: b[k] for k in DEVICE_FIELDS if k in b}
                    if "category" in fields and fields["category"] not in CATEGORIES:
                        raise ValueError("category")
                    if fields:
                        sets = ", ".join(f"{k} = ?" for k in fields)
                        store.x(f"UPDATE devices SET {sets} WHERE id = ?", (*fields.values(), rid))
                        moved_only = set(fields) <= {"x", "y"}
                        if not moved_only:
                            store.log(who, f"Edited {dev['name']}: " + ", ".join(
                                f"{k}={fields[k]}" for k in fields if k not in ("x", "y")))
                    return self._send(200, {"ok": True})
                if method == "DELETE":
                    if dev["source"] in ("dante", "videohub"):
                        store.x("UPDATE devices SET hidden = 1 WHERE id = ?", (rid,))
                        store.log(who, f"Hid {dev['source']} device {dev['name']}")
                    else:
                        store.x("DELETE FROM devices WHERE id = ?", (rid,))
                        store.log(who, f"Deleted device {dev['name']}")
                    return self._send(200, {"ok": True})

            if res == "ports":
                if method == "POST":
                    kind, direction = b.get("kind", "analog"), b["direction"]
                    if kind not in KINDS or direction not in ("in", "out"):
                        raise ValueError("kind/direction")
                    if kind == "dante":
                        raise ApiError(400, "Dante ports come from the network; add a different type")
                    dev = store.one("SELECT name FROM devices WHERE id = ?", (b["device_id"],))
                    count = max(1, min(int(b.get("count", 1)), 128))
                    start = int(b.get("start", 1))
                    base = b["name"].strip()
                    ids = []
                    for i in range(count):
                        nm = f"{base} {start + i}" if count > 1 else base
                        ids.append(store.x("INSERT INTO ports(device_id, direction, kind, number, name, label) "
                                           "VALUES (?,?,?,?,?,?)",
                                           (b["device_id"], direction, kind, start + i if count > 1 else None,
                                            nm, b.get("label", ""))))
                    store.log(who, f"Added {count} {kind} {direction}put(s) '{base}' to {dev['name']}")
                    return self._send(201, {"ids": ids})
                port = store.one("SELECT p.*, d.name dev FROM ports p JOIN devices d ON d.id = p.device_id "
                                 "WHERE p.id = ?", (rid,))
                if not port:
                    raise ApiError(404, "no such port")
                if method == "PATCH":
                    if port["kind"] == "dante" and port["source"] != "library" and "name" in b:
                        raise ApiError(400, "Rename Dante channels in Dante Controller; set a label here instead")
                    if port["source"] == "videohub":
                        raise ApiError(400, "Videohub labels come from the hub; rename them in Videohub Setup")
                    store.x("UPDATE ports SET name = COALESCE(?, name), label = COALESCE(?, label) WHERE id = ?",
                            (b.get("name"), b.get("label"), rid))
                    store.log(who, f"Edited port {port['dev']} / {port['name']}")
                    return self._send(200, {"ok": True})
                if method == "DELETE":
                    if port["source"] not in ("manual", "library"):
                        raise ApiError(400, "Ports read from the network can't be deleted here")
                    store.x("DELETE FROM ports WHERE id = ?", (rid,))
                    store.log(who, f"Deleted port {port['dev']} / {port['name']}")
                    return self._send(200, {"ok": True})

            if res == "connections":
                if method == "POST" and action is None and rid is None and "ids" in b:
                    ids = [int(i) for i in b["ids"]]
                    marks = ",".join("?" * len(ids)) or "NULL"
                    store.x(f"UPDATE connections SET documented = 1 WHERE live = 1 AND id IN ({marks})", tuple(ids))
                    store.log(who, f"Accepted {len(ids)} live route(s) as documented")
                    return self._send(200, {"ok": True})
                if method == "POST":
                    fp, tp = int(b["from_port"]), int(b["to_port"])
                    pf = store.one("SELECT * FROM ports WHERE id = ?", (fp,))
                    pt = store.one("SELECT * FROM ports WHERE id = ?", (tp,))
                    if not pf or not pt or pf["direction"] != "out" or pt["direction"] != "in":
                        raise ApiError(400, "Patch must go from an output to an input")
                    if not can_patch(pf["kind"], pt["kind"]):
                        if "dante" in (pf["kind"], pt["kind"]):
                            raise ApiError(400, "Dante channels can only patch to Dante channels")
                        raise ApiError(400, f"Can't patch {pf['kind'].upper()} to {pt['kind'].upper()}: "
                                            "they carry different kinds of signal")
                    existing = store.one("SELECT id FROM connections WHERE from_port = ? AND to_port = ?", (fp, tp))
                    if existing:
                        store.x("UPDATE connections SET documented = 1, cable = ?, notes = ? WHERE id = ?",
                                (b.get("cable", ""), b.get("notes", ""), existing["id"]))
                        cid = existing["id"]
                    else:
                        cid = store.x("INSERT INTO connections(from_port, to_port, documented, cable, notes) "
                                      "VALUES (?,?,1,?,?)", (fp, tp, b.get("cable", ""), b.get("notes", "")))
                    store.log(who, f"Patched {_pname(store, fp)} -> {_pname(store, tp)}")
                    return self._send(201, {"id": cid})
                c = store.one("SELECT * FROM connections WHERE id = ?", (rid,))
                if not c:
                    raise ApiError(404, "no such connection")
                if method == "PATCH":
                    store.x("UPDATE connections SET documented = COALESCE(?, documented), cable = COALESCE(?, cable), "
                            "notes = COALESCE(?, notes) WHERE id = ?",
                            (b.get("documented"), b.get("cable"), b.get("notes"), rid))
                    store.log(who, f"Edited route {_pname(store, c['from_port'])} -> {_pname(store, c['to_port'])}")
                    return self._send(200, {"ok": True})
                if method == "DELETE":
                    if c["live"]:
                        store.x("UPDATE connections SET documented = 0 WHERE id = ?", (rid,))
                    else:
                        store.x("DELETE FROM connections WHERE id = ?", (rid,))
                    store.log(who, f"Removed route {_pname(store, c['from_port'])} -> {_pname(store, c['to_port'])}")
                    return self._send(200, {"ok": True})

            raise ApiError(404, "not found")

    return H


def _pname(store, port_id):
    r = store.one("SELECT d.name dev, p.name port FROM ports p JOIN devices d ON d.id = p.device_id WHERE p.id = ?",
                  (port_id,))
    return f"{r['dev']} / {r['port']}" if r else f"port {port_id}"


def make_server(store, sync, cfg, vh=None, on_quit=None, port=None):
    httpd = ThreadingHTTPServer((config.bind_address(cfg), port or cfg.get("port", 8760)),
                                make_handler(store, sync, cfg, vh, on_quit))
    httpd.daemon_threads = True
    return httpd
