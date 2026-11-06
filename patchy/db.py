"""SQLite store for Patchy.

Model: rooms -> devices -> ports; connections join an output port to an input port.
A connection is `documented` (someone put it on the map) and/or `live` (seen in the
last read of the receiving device). The difference between the two is "drift".
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY,
  room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  category TEXT DEFAULT 'audio',          -- audio | video | lighting | network | control
  make TEXT DEFAULT '', model TEXT DEFAULT '',
  ip TEXT DEFAULT '', location TEXT DEFAULT '', notes TEXT DEFAULT '',
  x REAL, y REAL,
  dante_name TEXT UNIQUE,                  -- set when the device is on Dante
  source TEXT DEFAULT 'manual',            -- manual | dante
  hidden INTEGER DEFAULT 0,
  last_seen REAL
);
CREATE TABLE IF NOT EXISTS ports (
  id INTEGER PRIMARY KEY,
  device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  direction TEXT NOT NULL,                 -- in | out
  kind TEXT NOT NULL DEFAULT 'analog',     -- analog | aes | dante | madi | sdi | hdmi | dmx | sacn | ethernet | usb | other
  number INTEGER,
  name TEXT NOT NULL,
  label TEXT DEFAULT '',
  source TEXT DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS ports_dev ON ports(device_id, direction, kind);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY,
  from_port INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
  to_port INTEGER NOT NULL REFERENCES ports(id) ON DELETE CASCADE,
  documented INTEGER DEFAULT 0,
  live INTEGER DEFAULT 0,
  live_label TEXT DEFAULT '', live_severity TEXT DEFAULT '', transport TEXT DEFAULT '',
  live_seen REAL,
  cable TEXT DEFAULT '', notes TEXT DEFAULT '',
  UNIQUE(from_port, to_port)
);
CREATE TABLE IF NOT EXISTS syncs (
  id INTEGER PRIMARY KEY, started REAL, finished REAL, ok INTEGER,
  devices INTEGER, subscriptions INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY, at REAL, who TEXT, what TEXT
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(connections)")}
        if "misses" not in cols:   # passes in a row where the receiver answered without this route
            self.conn.execute("ALTER TABLE connections ADD COLUMN misses INTEGER DEFAULT 0")
        dcols = {r[1] for r in self.conn.execute("PRAGMA table_info(devices)")}
        if "control_ip" not in dcols:   # e.g. a console's control network port, separate from Dante
            self.conn.execute("ALTER TABLE devices ADD COLUMN control_ip TEXT DEFAULT ''")
        if "merged_into" not in dcols:  # a duplicate card folded into another device
            self.conn.execute("ALTER TABLE devices ADD COLUMN merged_into INTEGER")
        if "ext_key" not in dcols:      # identity for non-Dante live sources, e.g. "videohub:<ip>"
            self.conn.execute("ALTER TABLE devices ADD COLUMN ext_key TEXT")
            self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS devices_ext_key ON devices(ext_key)")
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(ports)")}
        for col, decl in (("xp_live", "INTEGER"),   # router output: input port routed to it right now
                          ("xp_doc", "INTEGER"),    # router output: documented input
                          ("xp_locked", "INTEGER DEFAULT 0"), ("xp_seen", "REAL")):
            if col not in pcols:
                self.conn.execute(f"ALTER TABLE ports ADD COLUMN {col} {decl}")
        # Early versions filed unmatched devices in a real room called "Unassigned"; no room is the same thing.
        old = self.conn.execute("SELECT id FROM rooms WHERE name = 'Unassigned'").fetchone()
        if old:
            self.conn.execute("UPDATE devices SET room_id = NULL WHERE room_id = ?", (old[0],))
            self.conn.execute("DELETE FROM rooms WHERE id = ?", (old[0],))

    # -- helpers -----------------------------------------------------------
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    def tx(self):
        """Context manager for a write transaction."""
        store = self

        class _Tx:
            def __enter__(self_):
                store.lock.acquire()
                store.conn.execute("BEGIN")
                return store

            def __exit__(self_, et, ev, tb):
                try:
                    store.conn.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store.lock.release()
        return _Tx()

    def log(self, who, what):
        self.x("INSERT INTO changes(at, who, what) VALUES (?,?,?)", (time.time(), who, what))

    # -- reads -------------------------------------------------------------
    def state(self):
        return {
            "rooms": self.q("SELECT * FROM rooms ORDER BY sort, name"),
            "devices": self.q("SELECT * FROM devices WHERE hidden = 0 ORDER BY name"),
            "ports": self.q("SELECT p.* FROM ports p JOIN devices d ON d.id = p.device_id "
                            "WHERE d.hidden = 0 ORDER BY p.device_id, p.direction, p.kind, "
                            "COALESCE(p.number, 99999), p.name"),
            "connections": self.q("SELECT * FROM connections"),
            "last_sync": self.one("SELECT * FROM syncs ORDER BY id DESC LIMIT 1"),
            "last_ok_sync": self.one("SELECT * FROM syncs WHERE ok = 1 ORDER BY id DESC LIMIT 1"),
        }

    # -- rooms -------------------------------------------------------------
    def room_id(self, name, create=True):
        r = self.one("SELECT id FROM rooms WHERE name = ?", (name,))
        if r:
            return r["id"]
        if not create:
            return None
        n = self.one("SELECT COALESCE(MAX(sort), 0) + 1 AS n FROM rooms")["n"]
        return self.x("INSERT INTO rooms(name, sort) VALUES (?, ?)", (name, n))


def open_store(data_dir):
    return Store(os.path.join(data_dir, "patchy.db"))
