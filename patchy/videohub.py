"""Read-only Blackmagic Videohub sync (Videohub Ethernet protocol, TCP 9990).

On connect a hub sends its whole state (device info, labels, locks, routing) and then
"END PRELUDE". We read that and hang up. Nothing is ever sent to the hub.

Each hub becomes a device with numbered SDI in/out ports labelled from the hub. A hub's
crosspoints live on its output ports: xp_live = input currently routed, xp_doc = the
documented input. They differ when someone re-routes. Changes on *locked* outputs count
as drift. Unlocked outputs are re-routed during services all the time, so those are only shown.
"""
import logging
import re
import socket
import threading
import time

DEFAULT_LABEL = re.compile(r"^(input|output)\s*\d+$", re.I)


def read_hub(host, timeout=8):
    s = socket.create_connection((host, 9990), timeout=4)
    s.settimeout(1.0)
    buf, end = b"", time.time() + timeout
    try:
        while time.time() < end and b"END PRELUDE:" not in buf:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if b"VIDEO OUTPUT ROUTING:" not in buf:
        raise RuntimeError("hub did not send its routing table")
    return parse(buf.decode("utf-8", "replace"))


def parse(txt):
    blocks = {}
    for blk in txt.replace("\r", "").split("\n\n"):
        lines = blk.strip().splitlines()
        if lines and lines[0].endswith(":"):
            blocks[lines[0][:-1]] = lines[1:]

    def indexed(name):
        out = {}
        for line in blocks.get(name, []):
            a, _, b = line.partition(" ")
            if a.isdigit():
                out[int(a)] = b
        return out

    dev = dict(l.split(": ", 1) for l in blocks.get("VIDEOHUB DEVICE", []) if ": " in l)
    return {
        "model": dev.get("Model name", ""),
        "name": dev.get("Friendly name", ""),
        "inputs": int(dev.get("Video inputs", 0) or 0),
        "outputs": int(dev.get("Video outputs", 0) or 0),
        "in_labels": indexed("INPUT LABELS"),
        "out_labels": indexed("OUTPUT LABELS"),
        "routing": {o: int(i) for o, i in indexed("VIDEO OUTPUT ROUTING").items() if i.strip().isdigit()},
        "locks": indexed("VIDEO OUTPUT LOCKS"),      # U unlocked, O owned by someone, L locked
    }


def clean_label(label):
    label = (label or "").strip()
    return "" if DEFAULT_LABEL.match(label) else label


class VideohubSync:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.status = {}      # host -> {ok, at, error}
        self.lock = threading.Lock()

    def start_background(self):
        def loop():   # always running, so hubs added in Settings are picked up
            time.sleep(2)
            while True:
                if self.cfg.get("videohubs"):
                    self.sync_all()
                time.sleep(max(15, self.cfg.get("videohub_seconds", 60)))
        threading.Thread(target=loop, daemon=True, name="videohub-sync").start()

    def sync_all(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            hubs = list(self.cfg.get("videohubs", []))
            for host in [h for h in self.status if h not in {x["host"] for x in hubs}]:
                self.status.pop(host, None)   # removed in Settings
            for hub in hubs:
                try:
                    self.sync_hub(hub)
                    self.status[hub["host"]] = {"ok": True, "at": time.time(), "name": hub.get("name")}
                except Exception as e:  # noqa: BLE001
                    logging.warning("videohub %s: %s", hub.get("host"), e)
                    self.status[hub["host"]] = {"ok": False, "at": time.time(), "name": hub.get("name"), "error": str(e)[:200]}
        finally:
            self.lock.release()
        from .dante import DanteSync   # give newly found hubs a spot on the map
        DanteSync(self.store, {})._layout_new()

    def _port(self, did, direction, number, label):
        s = self.store
        p = s.one("SELECT id, label FROM ports WHERE device_id=? AND direction=? AND kind='sdi' AND number=?",
                  (did, direction, number))
        name = f"{'In' if direction == 'in' else 'Out'} {number}"
        if p:
            if p["label"] != label:
                s.x("UPDATE ports SET label=? WHERE id=?", (label, p["id"]))
            return p["id"]
        return s.x("INSERT INTO ports(device_id, direction, kind, number, name, label, source) "
                   "VALUES (?,?, 'sdi', ?,?,?, 'videohub')", (did, direction, number, name, label))

    def sync_hub(self, hub):
        info = read_hub(hub["host"])
        s, now, key = self.store, time.time(), "videohub:" + hub["host"]
        with s.tx():
            d = s.one("SELECT * FROM devices WHERE ext_key = ?", (key,))
            model = info["model"].replace("Blackmagic ", "")
            if d:
                did = d["id"]
                s.x("UPDATE devices SET last_seen=?, model=?, ip=? WHERE id=?", (now, model, hub["host"], did))
            else:
                rid = s.room_id(hub["room"]) if hub.get("room") else None
                did = s.x("INSERT INTO devices(room_id, name, category, make, model, ip, source, ext_key, last_seen) "
                          "VALUES (?,?, 'video', 'Blackmagic Design', ?,?, 'videohub', ?,?)",
                          (rid, hub.get("name") or info["name"], model, hub["host"], key, now))
                s.log("videohub-sync", f"New Videohub: {hub.get('name') or info['name']} ({hub['host']})")
            ins = {n: self._port(did, "in", n + 1, clean_label(info["in_labels"].get(n)))
                   for n in range(info["inputs"])}
            outs = {n: self._port(did, "out", n + 1, clean_label(info["out_labels"].get(n)))
                    for n in range(info["outputs"])}
            for o, i in info["routing"].items():
                if o not in outs or i not in ins:
                    continue
                locked = 1 if info["locks"].get(o, "U").strip() in ("O", "L") else 0
                # first sighting of a crosspoint documents it as-is
                s.x("UPDATE ports SET xp_live=?, xp_locked=?, xp_seen=?, xp_doc=COALESCE(xp_doc, ?) WHERE id=?",
                    (ins[i], locked, now, ins[i], outs[o]))
