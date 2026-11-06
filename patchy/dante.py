"""Read-only Dante sync through the netaudio CLI.

Each mDNS discovery pass only catches part of the network, so results are merged:
devices are never removed, and a route is only marked gone when its receiving
device answered in this pass and no longer has it.
"""
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

SEVERITY_RANK = {"ok": 0, "info": 0, "": 0, None: 0}


def _strip_local(name):
    return re.sub(r"\.local\.?$", "", name or "")


def _child_cmd():
    """How to start this app again as a netaudio child process."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
    return [sys.executable, run_py]


def run_netaudio(args, timeout_s, interface=None):
    """Run one read-only netaudio command and return its JSON output."""
    opts = ["--json", "--no-color", "--timeout", str(timeout_s), "--log-level", "ERROR"]
    if interface:
        opts += ["--interface", interface]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    last_err = ""
    for _ in range(2):   # mDNS discovery is noisy; one retry
        fd, out = tempfile.mkstemp(prefix="patchy-netaudio-", suffix=".json")
        os.close(fd)
        try:
            p = subprocess.run(_child_cmd() + ["--netaudio-child", out] + opts + args,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout_s * 6 + 60, env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with open(out, encoding="utf-8", errors="replace") as f:
                txt = f.read().lstrip("﻿").strip()
            if txt:
                return json.loads(txt)
            try:
                with open(out + ".err", encoding="utf-8", errors="replace") as f:
                    last_err = f.read()[-400:] or f"exit code {p.returncode}"
            except OSError:
                last_err = f"exit code {p.returncode}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        finally:
            for f in (out, out + ".err"):
                try:
                    os.remove(f)
                except OSError:
                    pass
    raise RuntimeError(f"netaudio {' '.join(args)} failed: {last_err.strip()}")


_NOT_DANTE = [ipaddress.ip_network(n) for n in ("100.64.0.0/10",   # Tailscale / CGNAT
                                                 "169.254.0.0/16",  # link-local
                                                 "127.0.0.0/8")]


def usable_ip(ip):
    """A device's Dante address, or '' when mDNS answered over a VPN or link-local interface
    (e.g. Dante Virtual Soundcard on this PC also answering on Tailscale)."""
    try:
        a = ipaddress.ip_address(ip or "")
    except ValueError:
        return ""
    return "" if any(a in n for n in _NOT_DANTE) else str(a)


def yamaha_devinfo(ip, timeout=3):
    """Read-only Yamaha RCP query on TCP 49280: product name and unit ID."""
    out = {}
    try:
        with socket.create_connection((ip, 49280), timeout=timeout) as sock:
            sock.settimeout(timeout)
            buf = b""
            for key in ("productname", "deviceid"):
                sock.sendall(f"devinfo {key}\n".encode())
                while b"\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return out
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                m = re.match(r'OK devinfo \w+ "([^"]*)"', line.decode("utf-8", "replace"))
                if m:
                    out[key] = m.group(1)
    except OSError:
        pass
    return out


def room_for(name, rules):
    for pattern, room in rules:
        if re.search(pattern, name, re.I):
            return room
    return None   # no room: shows under "Unassigned"


class DanteSync:
    def __init__(self, store, cfg):
        self.store = store
        self.cfg = cfg
        self.running = False
        self.lock = threading.Lock()

    # -- public --------------------------------------------------------------
    def start_background(self):
        def loop():
            time.sleep(3)
            while True:
                if self.cfg.get("dante_enabled", True):
                    self.sync("scheduler")
                time.sleep(max(1, self.cfg.get("sync_minutes", 5)) * 60)
        threading.Thread(target=loop, daemon=True, name="dante-sync").start()

    def sync_async(self, who):
        if self.running or not self.cfg.get("dante_enabled", True):
            return False
        threading.Thread(target=self.sync, args=(who,), daemon=True).start()
        return True

    def sync(self, who="manual"):
        if not self.lock.acquire(blocking=False):
            return
        self.running = True
        started = time.time()
        sid = self.store.x("INSERT INTO syncs(started, ok, message) VALUES (?, 0, 'running')", (started,))
        try:
            t = self.cfg.get("netaudio_timeout", 8)
            iface = self.cfg.get("netaudio_interface")
            channels = run_netaudio(["channel", "list"], t, iface)
            # --all lists every rx channel of each receiver that answered, so an
            # unsubscribed channel is a real "no route", not a missed reply.
            subs = run_netaudio(["subscription", "list", "--all"], t, iface)
            try:
                meta = run_netaudio(["device", "list"], t, iface).get("dante", {})
            except Exception:  # noqa: BLE001 - metadata is optional
                meta = {}
            n_dev, n_sub = self._merge(channels, subs, meta)
            self._fold_console_control()
            self._layout_new()
            self.store.x("UPDATE syncs SET finished=?, ok=1, devices=?, subscriptions=?, message=? WHERE id=?",
                         (time.time(), n_dev, n_sub, f"by {who}", sid))
        except Exception as e:  # noqa: BLE001
            self.store.x("UPDATE syncs SET finished=?, ok=0, message=? WHERE id=?",
                         (time.time(), str(e)[:500], sid))
        finally:
            self.running = False
            self.lock.release()

    # -- merge ---------------------------------------------------------------
    def _device(self, name, now, meta=None):
        s = self.store
        d = s.one("SELECT * FROM devices WHERE dante_name = ?", (name,))
        m = meta or {}
        make = m.get("manufacturer") or ""
        model = m.get("product_name") or m.get("platform_model_name") or m.get("model") or ""
        ip = usable_ip(m.get("ipv4"))
        if d:
            s.x("UPDATE devices SET last_seen=COALESCE(?, last_seen), ip=CASE WHEN ?<>'' THEN ? ELSE ip END, "
                "make=CASE WHEN make='' THEN ? ELSE make END, model=CASE WHEN model='' THEN ? ELSE model END "
                "WHERE id=?", (now, ip, ip, make, model, d["id"]))
            return d["id"]
        room = room_for(name, self.cfg.get("room_rules", []))
        rid = s.room_id(room) if room else None
        did = s.x("INSERT INTO devices(room_id, name, category, make, model, ip, dante_name, source, last_seen) "
                  "VALUES (?,?,?,?,?,?,?,'dante',?)", (rid, name, "audio", make, model, ip, name, now))
        s.log("dante-sync", f"New Dante device found: {name}")
        return did

    def _port(self, device_id, direction, name, number=None):
        s = self.store
        p = None
        if number is not None:
            p = s.one("SELECT * FROM ports WHERE device_id=? AND direction=? AND kind='dante' AND number=?",
                      (device_id, direction, number))
        if not p:
            p = s.one("SELECT * FROM ports WHERE device_id=? AND direction=? AND kind='dante' AND name=?",
                      (device_id, direction, name))
        if p:
            if p["name"] != name or (number is not None and p["number"] != number):
                s.x("UPDATE ports SET name=?, number=COALESCE(?, number) WHERE id=?", (name, number, p["id"]))
            return p["id"]
        return s.x("INSERT INTO ports(device_id, direction, kind, number, name, source) VALUES (?,?, 'dante', ?,?, 'dante')",
                   (device_id, direction, number, name))

    def _merge(self, channels, subs, meta):
        s = self.store
        now = time.time()
        first_syncs = s.one("SELECT COUNT(*) AS n FROM syncs WHERE ok = 1")["n"]
        baseline = first_syncs < self.cfg.get("auto_baseline_syncs", 3)
        meta_by = {_strip_local(k): v for k, v in (meta or {}).items()}
        seen_rx = set()        # receivers whose subscription table came back this pass
        rx_rows = {}
        for sub in subs or []:
            if sub.get("rx_device"):
                rx_rows[sub["rx_device"]] = rx_rows.get(sub["rx_device"], 0) + 1
        with s.tx():
            dev_ids = {}
            for key, ch in (channels or {}).items():
                name = ch.get("name") or _strip_local(key)
                did = self._device(name, now, meta_by.get(name))
                dev_ids[name] = did
                for direction, field in (("out", "tx_channels"), ("in", "rx_channels")):
                    for c in (ch.get(field) or {}).values():
                        self._port(did, direction, c.get("name") or str(c.get("number")), c.get("number"))
            for name, m in meta_by.items():
                if name not in dev_ids:
                    dev_ids[name] = self._device(name, now, m)

            live_now = set()
            n_sub = 0
            for rx_dev, n in rx_rows.items():
                rx_id = dev_ids.get(rx_dev) or self._device(rx_dev, now, meta_by.get(rx_dev))
                dev_ids[rx_dev] = rx_id
                known = s.one("SELECT COUNT(*) n FROM ports WHERE device_id=? AND direction='in' AND kind='dante'",
                              (rx_id,))["n"]
                if n >= known:   # full table: safe to judge missing routes
                    seen_rx.add(rx_id)
            for sub in subs or []:
                rx_dev, tx_dev = sub.get("rx_device"), sub.get("tx_device")
                if not rx_dev or not tx_dev or not sub.get("tx_channel"):
                    continue
                rx_id = dev_ids[rx_dev]
                tx_id = dev_ids.get(tx_dev) or self._device(tx_dev, None, meta_by.get(tx_dev))
                dev_ids.setdefault(tx_dev, tx_id)
                rx_p = self._port(rx_id, "in", sub.get("rx_channel"), sub.get("rx_channel_number"))
                tx_p = self._port(tx_id, "out", sub["tx_channel"])
                st = sub.get("status") or {}
                label = st.get("label") or ""
                sev = st.get("severity") or ""
                transport = st.get("transport") or ""
                c = s.one("SELECT id FROM connections WHERE from_port=? AND to_port=?", (tx_p, rx_p))
                if c:
                    s.x("UPDATE connections SET live=1, misses=0, live_label=?, live_severity=?, transport=?, live_seen=?"
                        + (", documented=1" if baseline else "") + " WHERE id=?",
                        (label, sev, transport, now, c["id"]))
                    cid = c["id"]
                else:
                    cid = s.x("INSERT INTO connections(from_port, to_port, documented, live, live_label, "
                              "live_severity, transport, live_seen) VALUES (?,?,?,1,?,?,?,?)",
                              (tx_p, rx_p, 1 if baseline else 0, label, sev, transport, now))
                live_now.add(cid)
                n_sub += 1

            # Routes into receivers that answered this pass but are no longer subscribed.
            if seen_rx:
                marks = ",".join("?" * len(seen_rx))
                gone = s.q(f"SELECT c.id, c.misses FROM connections c JOIN ports p ON p.id = c.to_port "
                           f"WHERE c.live = 1 AND p.kind = 'dante' AND p.device_id IN ({marks})", tuple(seen_rx))
                for g in gone:
                    if g["id"] not in live_now:
                        # two full reads in a row without it before calling it gone
                        s.x("UPDATE connections SET misses = misses + 1, live = CASE WHEN misses + 1 >= 2 "
                            "THEN 0 ELSE 1 END WHERE id = ?", (g["id"],))
                # Undocumented routes that vanished are just noise: drop them.
                s.x("DELETE FROM connections WHERE live = 0 AND documented = 0 AND cable = '' AND notes = ''")
        return len(dev_ids), n_sub

    def _fold_console_control(self):
        """Yamaha consoles (DM7/DM7C...) also announce their control network port on Dante,
        named after its MAC and with no channels. Ask it who it is over Yamaha RCP (TCP 49280,
        read-only `devinfo` queries) and fold it into the console's Dante card as its control IP.
        The console's default Dante name starts with Y + its unit ID in 3 hex digits (Y001...)."""
        s = self.store
        cands = s.q("SELECT d.* FROM devices d WHERE d.hidden = 0 AND d.merged_into IS NULL AND d.ip <> '' "
                    "AND d.dante_name GLOB '[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]"
                    "[0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F][0-9A-F]' "
                    "AND NOT EXISTS (SELECT 1 FROM ports p WHERE p.device_id = d.id)")
        for d in cands:
            info = yamaha_devinfo(d["ip"])
            if not info.get("deviceid"):
                continue
            try:
                prefix = "Y%03X" % int(info["deviceid"])
            except ValueError:
                continue
            # Unit IDs are only unique per product type (a DM7 and an RUio16 can both be Y001),
            # so the product must match too: model first, then the Dante name.
            prod = (info.get("productname") or "").upper()
            same_id = s.q("SELECT * FROM devices WHERE id <> ? AND merged_into IS NULL AND dante_name LIKE ?",
                          (d["id"], prefix + "-%"))
            exact = [c for c in same_id if prod and (c["model"] or "").upper() == prod]
            named = [c for c in same_id if prod and not c["model"] and re.search(rf"(^|-){re.escape(prod)}(-|$)",
                                                                                (c["dante_name"] or "").upper())]
            pick = exact or named
            if len(pick) != 1:
                continue   # ambiguous or unknown: leave the card for a person to sort out
            console = pick[0]
            with s.tx():
                s.x("UPDATE devices SET control_ip = ?, model = CASE WHEN model = '' THEN ? ELSE model END WHERE id = ?",
                    (d["ip"], info.get("productname", ""), console["id"]))
                s.x("UPDATE devices SET hidden = 1, merged_into = ? WHERE id = ?", (console["id"], d["id"]))
                s.log("dante-sync", f"Folded {d['name']} ({info.get('productname', '?')} unit {info['deviceid']}, "
                                    f"control {d['ip']}) into {console['name']}")

    def _layout_new(self):
        """Give new devices a starting spot: senders left, receivers right."""
        s = self.store
        new = s.q("SELECT id, room_id FROM devices WHERE x IS NULL")
        for d in new:
            outs = s.one("SELECT COUNT(*) n FROM ports WHERE device_id=? AND direction='out'", (d["id"],))["n"]
            ins = s.one("SELECT COUNT(*) n FROM ports WHERE device_id=? AND direction='in'", (d["id"],))["n"]
            col = 0 if outs and not ins else 2 if ins and not outs else 1
            x = 60 + col * 340
            ys = s.one("SELECT MAX(y) m FROM devices WHERE room_id IS ? AND x = ?", (d["room_id"], x))["m"]
            y = 60 if ys is None else ys + 110
            s.x("UPDATE devices SET x=?, y=? WHERE id=?", (x, y, d["id"]))
