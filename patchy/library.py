"""Built-in device library: what each model's I/O looks like, so a device can be added
offline (no network) with its local and Dante ports already in place.

The library is static/library.json. Each model lists port groups and optional slots:

  {"id": "yamaha-rio3224-d2", "make": "Yamaha", "model": "Rio3224-D2", "category": "audio",
   "ports": [{"dir": "in", "kind": "analog", "count": 32, "name": "Input"}, ...],
   "options": [{"slot": "Slot", "choices": [{"name": "NY64-D (Dante)", "ports": [...]}]}]}

A choice with "separate": true is its own Dante device (e.g. a Dante card in a console's
expansion slot next to the console's built-in Dante), so it becomes a second device.

Dante ports added here are placeholders (source 'library'). When the device is linked to
its name on the Dante network, the live channels take over the placeholders by number.
"""
import json
import os

from . import paths

_cache = {"mtime": None, "data": None}


def load():
    p = os.path.join(paths.static_dir(), "library.json")
    m = os.path.getmtime(p)
    if _cache["mtime"] != m:
        with open(p, encoding="utf-8") as f:
            _cache["data"] = json.load(f)
        _cache["mtime"] = m
    return _cache["data"]


def model(model_id):
    for m in load()["models"]:
        if m["id"] == model_id:
            return m
    raise ValueError(f"unknown model {model_id}")


def _expand(groups):
    """Port groups -> individual ports (direction, kind, number, name)."""
    out = []
    for g in groups:
        n = int(g.get("count", 1))
        start = int(g.get("start", 1))
        for i in range(n):
            num = start + i
            name = f"{g['name']} {num}" if n > 1 or g.get("numbered") else g["name"]
            out.append((g["dir"], g["kind"], num, name))
    return out


def plan(model_id, choices):
    """What adding this model creates: (main ports, [(suffix, ports) for separate devices])."""
    m = model(model_id)
    groups, extra = list(m.get("ports", [])), []
    for i, opt in enumerate(m.get("options", [])):
        pick = choices.get(str(i), choices.get(i))
        if pick in (None, "", -1, "-1"):
            continue
        ch = opt["choices"][int(pick)]
        if ch.get("separate"):
            extra.append((f"{opt['slot']} {ch['name']}", _expand(ch.get("ports", []))))
        else:
            groups += ch.get("ports", [])
    return m, _expand(groups), extra


def add_ports(store, device_id, ports, skip_dante=False):
    """Insert ports, skipping any the device already has (same direction, type and name),
    and Dante ports altogether when the device's Dante channels come from the network."""
    have = {(r["direction"], r["kind"], r["name"]) for r in
            store.q("SELECT direction, kind, name FROM ports WHERE device_id = ?", (device_id,))}
    n = 0
    for direction, kind, number, name in ports:
        if (direction, kind, name) in have or (kind == "dante" and skip_dante):
            continue
        store.x("INSERT INTO ports(device_id, direction, kind, number, name, source) VALUES (?,?,?,?,?,?)",
                (device_id, direction, kind, number, name, "library"))
        n += 1
    return n


def apply(store, device_id, model_id, choices, who):
    """Give an existing device a model's ports (and create devices for separate cards)."""
    dev = store.one("SELECT * FROM devices WHERE id = ?", (device_id,))
    m, ports, extra = plan(model_id, choices or {})
    with store.tx():
        store.x("UPDATE devices SET make = CASE WHEN make = '' THEN ? ELSE make END, "
                "model = CASE WHEN model = '' THEN ? ELSE model END WHERE id = ?", (m["make"], m["model"], device_id))
        # A device already read from Dante has its real channels; don't add placeholders next to them.
        live_dante = store.one("SELECT COUNT(*) n FROM ports WHERE device_id = ? AND kind = 'dante' AND source = 'dante'",
                               (device_id,))["n"] > 0
        n = add_ports(store, device_id, ports, skip_dante=live_dante)
        new_ids = []
        for i, (suffix, cports) in enumerate(extra):
            nid = store.x("INSERT INTO devices(room_id, name, category, make, model, location, x, y) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (dev["room_id"], f"{dev['name']} {suffix}", dev["category"], m["make"],
                           f"{m['model']} {suffix}", dev["location"],
                           (dev["x"] or 60), (dev["y"] or 60) + 110 * (i + 1)))
            add_ports(store, nid, cports)
            new_ids.append(nid)
        store.log(who, f"Added {m['make']} {m['model']} ports to {dev['name']} ({n} port(s)"
                       + (f", plus {len(new_ids)} card device(s)" if new_ids else "") + ")")
    return {"ports": n, "devices": new_ids}


def link_dante(store, device_id, dante_name, who):
    """Tie a device to its name on the Dante network. If Dante already found it as a separate
    card, fold that card in: its channels replace the placeholders with the same number, and
    its routes move across."""
    dante_name = (dante_name or "").strip() or None
    dev = store.one("SELECT * FROM devices WHERE id = ?", (device_id,))
    with store.tx():
        if dante_name is None:
            store.x("UPDATE devices SET dante_name = NULL WHERE id = ?", (device_id,))
            store.log(who, f"Unlinked {dev['name']} from Dante")
            return
        other = store.one("SELECT * FROM devices WHERE dante_name = ? AND id <> ?", (dante_name, device_id))
        if other and other["source"] != "dante":
            raise ValueError(f"{dante_name} is already linked to {other['name']}")
        if other:
            for p in store.q("SELECT * FROM ports WHERE device_id = ?", (other["id"],)):
                t = None
                if p["kind"] == "dante" and p["number"] is not None:
                    t = store.one("SELECT * FROM ports WHERE device_id = ? AND direction = ? AND kind = 'dante' "
                                  "AND number = ?", (device_id, p["direction"], p["number"]))
                if not t:
                    store.x("UPDATE ports SET device_id = ? WHERE id = ?", (device_id, p["id"]))
                    continue
                store.x("UPDATE ports SET name = ?, source = 'dante', label = CASE WHEN label = '' THEN ? ELSE label END "
                        "WHERE id = ?", (p["name"], p["label"], t["id"]))
                _move_routes(store, p["id"], t["id"])
                store.x("DELETE FROM ports WHERE id = ?", (p["id"],))
            store.x("UPDATE devices SET dante_name = NULL WHERE id = ?", (other["id"],))
            store.x("UPDATE devices SET ip = CASE WHEN ip = '' THEN ? ELSE ip END, "
                    "control_ip = CASE WHEN control_ip = '' THEN ? ELSE control_ip END, last_seen = ? WHERE id = ?",
                    (other["ip"], other["control_ip"] or "", other["last_seen"], device_id))
            store.x("UPDATE devices SET merged_into = ? WHERE merged_into = ?", (device_id, other["id"]))
            store.x("DELETE FROM devices WHERE id = ?", (other["id"],))
        # The device stays 'manual' (hand-added): only cards Dante found by itself can be folded in like this.
        store.x("UPDATE devices SET dante_name = ? WHERE id = ?", (dante_name, device_id))
        store.log(who, f"Linked {dev['name']} to Dante device {dante_name}"
                       + (" and merged the card Dante had found" if other else ""))


def _move_routes(store, old, new):
    for col, other_col in (("from_port", "to_port"), ("to_port", "from_port")):
        for c in store.q(f"SELECT * FROM connections WHERE {col} = ?", (old,)):
            dup = store.one(f"SELECT * FROM connections WHERE {col} = ? AND {other_col} = ?", (new, c[other_col]))
            if dup:
                store.x("UPDATE connections SET documented = MAX(documented, ?), live = MAX(live, ?), "
                        "live_label = ?, live_severity = ?, transport = ?, live_seen = ?, "
                        "cable = CASE WHEN cable = '' THEN ? ELSE cable END, "
                        "notes = CASE WHEN notes = '' THEN ? ELSE notes END WHERE id = ?",
                        (c["documented"], c["live"], c["live_label"], c["live_severity"], c["transport"],
                         c["live_seen"], c["cable"], c["notes"], dup["id"]))
                store.x("DELETE FROM connections WHERE id = ?", (c["id"],))
            else:
                store.x(f"UPDATE connections SET {col} = ? WHERE id = ?", (new, c["id"]))
