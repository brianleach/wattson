"""SQLite storage for snapshots, trips, and idle-drain events."""
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    soc REAL,
    capacity_kwh REAL,
    range_mi REAL,
    charger_state TEXT,
    charge_port TEXT,
    power_kw REAL,
    power_state TEXT,
    gear TEXT,
    lat REAL,
    lon REAL,
    speed REAL,
    geo_label TEXT,
    odometer REAL,
    cloud_online INTEGER,
    cloud_sync_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);

-- Full raw API payload per poll, kept so fields can be re-derived later
-- even if the normalizer didn't capture them at the time.
CREATE TABLE IF NOT EXISTS raw_snapshots(
    ts INTEGER PRIMARY KEY,
    raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips(
    id INTEGER PRIMARY KEY,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    miles REAL,
    kwh_used REAL,
    wh_per_mi REAL,
    avg_mph REAL,
    start_lat REAL,
    start_lon REAL,
    end_lat REAL,
    end_lon REAL,
    start_label TEXT,
    end_label TEXT,
    bracketed INTEGER DEFAULT 0,
    via_lat REAL,
    via_lon REAL,
    via_label TEXT,
    path TEXT,
    temp_f REAL
);

CREATE TABLE IF NOT EXISTS errands(
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL UNIQUE,
    drives INTEGER,
    miles REAL,
    kwh_used REAL
);

CREATE TABLE IF NOT EXISTS charges(
    id INTEGER PRIMARY KEY,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    minutes REAL,
    soc_start REAL,
    soc_end REAL,
    kwh_added REAL,
    miles_added REAL,
    avg_kw REAL,
    charge_type TEXT,
    lat REAL,
    lon REAL,
    label TEXT,
    user_cost_per_kwh REAL
);

CREATE TABLE IF NOT EXISTS drains(
    id INTEGER PRIMARY KEY,
    sleep_ts INTEGER NOT NULL,
    wake_ts INTEGER NOT NULL,
    hours REAL,
    soc_start REAL,
    soc_end REAL,
    kwh_lost REAL,
    watts_avg REAL,
    cd_kwh REAL,
    cd_watts REAL,
    steady_kwh REAL,
    steady_watts REAL,
    steady_hours REAL,
    range_start REAL,
    range_end REAL,
    wake_count INTEGER
);

CREATE TABLE IF NOT EXISTS places(
    lat_r INTEGER NOT NULL,
    lon_r INTEGER NOT NULL,
    label TEXT,
    ts INTEGER,
    PRIMARY KEY (lat_r, lon_r)
);

-- Every completed charging session from Rivian's charging API. Paid ones
-- price our DC charges (re-applied after every re-derive); ones that overlap
-- no charge of ours are sessions we never tracked, counted at report time.
CREATE TABLE IF NOT EXISTS rivian_sessions(
    transaction_id TEXT PRIMARY KEY,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    kwh REAL,
    paid_total REAL,
    currency TEXT,
    vendor TEXT,
    city TEXT,
    range_added_km REAL,
    is_home INTEGER,
    is_public INTEGER
);

-- Ambient temperature cache for trips (Open-Meteo, hourly): one row per
-- (local day, hour, ~11 km cell). Filled on demand by weather.temp_for_trip.
CREATE TABLE IF NOT EXISTS weather_cache(
    day TEXT NOT NULL,
    hour INTEGER NOT NULL,
    lat_r REAL NOT NULL,
    lon_r REAL NOT NULL,
    temp_f REAL,
    PRIMARY KEY (day, hour, lat_r, lon_r)
);
"""


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        # Lightweight migrations for tables created before new columns.
        cols = {r["name"] for r in
                con.execute("PRAGMA table_info(drains)").fetchall()}
        for col in ("cd_kwh", "cd_watts", "steady_kwh", "steady_watts",
                    "steady_hours", "wake_count"):
            if col not in cols:
                con.execute(f"ALTER TABLE drains ADD COLUMN {col} REAL")
        snap_cols = {r["name"] for r in
                     con.execute("PRAGMA table_info(snapshots)").fetchall()}
        if "speed" not in snap_cols:
            con.execute("ALTER TABLE snapshots ADD COLUMN speed REAL")
        for col in ("cloud_online INTEGER", "cloud_sync_ts INTEGER"):
            name = col.split()[0]
            if name not in snap_cols:
                con.execute(f"ALTER TABLE snapshots ADD COLUMN {col}")
        trip_cols = {r["name"] for r in
                     con.execute("PRAGMA table_info(trips)").fetchall()}
        if "bracketed" not in trip_cols:
            con.execute(
                "ALTER TABLE trips ADD COLUMN bracketed INTEGER DEFAULT 0")
        for col in ("via_lat REAL", "via_lon REAL", "via_label TEXT",
                    "path TEXT", "avg_mph REAL", "temp_f REAL"):
            name = col.split()[0]
            if name not in trip_cols:
                con.execute(f"ALTER TABLE trips ADD COLUMN {col}")
        charge_cols = {r["name"] for r in
                       con.execute("PRAGMA table_info(charges)").fetchall()}
        if "user_cost_per_kwh" not in charge_cols:
            con.execute("ALTER TABLE charges ADD COLUMN user_cost_per_kwh REAL")


def last_odometer():
    with connect() as con:
        row = con.execute(
            "SELECT odometer FROM snapshots WHERE odometer IS NOT NULL"
            " ORDER BY ts DESC LIMIT 1").fetchone()
        return row["odometer"] if row else None


def insert_snapshot(s: dict) -> None:
    # Odometer is cached server-side and sometimes comes back null; carry the
    # last known reading forward (it can never decrease while parked anyway).
    if s.get("odometer") is None:
        s["odometer"] = last_odometer()
    with connect() as con:
        con.execute(
            """INSERT INTO snapshots
               (ts, soc, capacity_kwh, range_mi, charger_state, charge_port,
                power_kw, power_state, gear, lat, lon, speed, geo_label, odometer,
                cloud_online, cloud_sync_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["ts"], s.get("soc"), s.get("capacity_kwh"), s.get("range_mi"),
             s.get("charger_state"), s.get("charge_port"), s.get("power_kw"),
             s.get("power_state"), s.get("gear"),
             s.get("lat"), s.get("lon"), s.get("speed"), s.get("geo_label"),
             s.get("odometer"), s.get("cloud_online"), s.get("cloud_sync_ts")),
        )


def insert_raw_snapshot(ts: int, raw: dict) -> None:
    """Keep the full raw API payload for a poll. Never deleted; lets us
    re-derive fields the normalizer didn't capture at the time."""
    import json
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO raw_snapshots (ts, raw) VALUES (?,?)",
            (ts, json.dumps(raw)),
        )


def get_place(lat_r: int, lon_r: int):
    with connect() as con:
        row = con.execute(
            "SELECT label FROM places WHERE lat_r=? AND lon_r=?",
            (lat_r, lon_r)).fetchone()
        return row["label"] if row else None


def get_place_entry(lat_r: int, lon_r: int):
    """(label, ts) if this cell was ever looked up (label may be None), else None."""
    with connect() as con:
        row = con.execute(
            "SELECT label, ts FROM places WHERE lat_r=? AND lon_r=?",
            (lat_r, lon_r)).fetchone()
        return (row["label"], row["ts"]) if row else None


def set_place(lat_r: int, lon_r: int, label: str | None, ts: int) -> None:
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO places (lat_r, lon_r, label, ts)"
            " VALUES (?,?,?,?)", (lat_r, lon_r, label, ts))


def upsert_rivian_sessions(sessions: list[dict]) -> None:
    with connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO rivian_sessions (transaction_id, start_ts,"
            " end_ts, kwh, paid_total, currency, vendor, city,"
            " range_added_km, is_home, is_public)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(r["transaction_id"], r["start_ts"], r["end_ts"], r["kwh"],
              r["paid_total"], r["currency"], r["vendor"], r["city"],
              r["range_added_km"], r["is_home"], r["is_public"])
             for r in sessions])
        apply_rivian_sessions(con)


def apply_rivian_sessions(con) -> None:
    """Price DC charges from the paid session they overlap most in time.

    Matching runs against every DC charge, priced or not, so a session whose
    charge already has a manual price can't drift onto a neighbor. Only
    unpriced charges are written: a manual entry always wins. The rate uses
    our battery-side kWh, not the billed kWh, so the session's EV cost
    equals what was paid.
    """
    paid = con.execute(
        "SELECT start_ts, end_ts, paid_total FROM rivian_sessions"
        " WHERE paid_total IS NOT NULL AND currency = 'USD'").fetchall()
    charges = con.execute(
        "SELECT id, start_ts, end_ts, kwh_added, user_cost_per_kwh"
        " FROM charges WHERE lower(charge_type) LIKE '%dc%'").fetchall()
    pairs = []
    for ri, r in enumerate(paid):
        for c in charges:
            overlap = (min(r["end_ts"], c["end_ts"])
                       - max(r["start_ts"], c["start_ts"]))
            if overlap > 0:
                pairs.append((overlap, ri, c))
    used_paid, used_charges = set(), set()
    for _, ri, c in sorted(pairs, key=lambda p: p[0], reverse=True):
        if ri in used_paid or c["id"] in used_charges:
            continue
        used_paid.add(ri)
        used_charges.add(c["id"])
        if c["user_cost_per_kwh"] is None and (c["kwh_added"] or 0) > 0:
            con.execute(
                "UPDATE charges SET user_cost_per_kwh = ? WHERE id = ?",
                (paid[ri]["paid_total"] / c["kwh_added"], c["id"]))


def latest_unpriced_dc_end(since: int) -> int | None:
    """End time of the newest unpriced DC charge that ended after ``since``."""
    with connect() as con:
        return con.execute(
            "SELECT MAX(end_ts) FROM charges WHERE lower(charge_type) LIKE"
            " '%dc%' AND user_cost_per_kwh IS NULL AND kwh_added > 0"
            " AND end_ts > ?", (since,)).fetchone()[0]


def unmatched_rivian_sessions() -> list[dict]:
    """Rivian sessions that overlap no charge of ours, newest first."""
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM rivian_sessions s WHERE NOT EXISTS ("
            " SELECT 1 FROM charges c"
            " WHERE min(s.end_ts, c.end_ts) - max(s.start_ts, c.start_ts) > 0)"
            " ORDER BY start_ts DESC")]


def rebuild_derived(trips: list[dict], drains: list[dict],
                    charges: list[dict] | None = None,
                    errands: list[dict] | None = None) -> None:
    init()  # ensure schema + migrations before wiping derived tables
    with connect() as con:
        # Preserve user-entered DC fast-charge costs across the re-derive:
        # charges are deleted and re-inserted below, keyed by start_ts.
        try:
            _costs = {r[0]: r[1] for r in con.execute(
                "SELECT start_ts, user_cost_per_kwh FROM charges "
                "WHERE user_cost_per_kwh IS NOT NULL").fetchall()}
        except Exception:
            _costs = {}
        con.execute("DELETE FROM trips")
        con.execute("DELETE FROM drains")
        con.execute("DELETE FROM charges")
        con.execute("DELETE FROM errands")
        con.executemany(
            "INSERT INTO trips (start_ts, end_ts, miles, kwh_used, wh_per_mi,"
            " avg_mph, start_lat, start_lon, end_lat, end_lon, start_label,"
            " end_label, bracketed, via_lat, via_lon, via_label, path,"
            " temp_f)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(t["start_ts"], t["end_ts"], t["miles"], t["kwh_used"],
              t["wh_per_mi"], t.get("avg_mph"),
              t.get("start_lat"), t.get("start_lon"),
              t.get("end_lat"), t.get("end_lon"),
              t.get("start_label"), t.get("end_label"),
              1 if t.get("bracketed") else 0,
              t.get("via_lat"), t.get("via_lon"), t.get("via_label"),
              t.get("path"), t.get("temp_f")) for t in trips],
        )
        con.executemany(
            "INSERT INTO drains (sleep_ts, wake_ts, hours, soc_start, soc_end,"
            " kwh_lost, watts_avg, cd_kwh, cd_watts, steady_kwh, steady_watts,"
            " steady_hours, range_start, range_end, wake_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(d["sleep_ts"], d["wake_ts"], d["hours"], d["soc_start"],
              d["soc_end"], d["kwh_lost"], d["watts_avg"], d.get("cd_kwh"),
              d.get("cd_watts"), d.get("steady_kwh"), d.get("steady_watts"),
              d.get("steady_hours"),
              d.get("range_start"), d.get("range_end"),
              d.get("wake_count", 0)) for d in drains],
        )
        con.executemany(
            "INSERT INTO charges (start_ts, end_ts, minutes, soc_start,"
            " soc_end, kwh_added, miles_added, avg_kw, charge_type,"
            " lat, lon, label)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(c["start_ts"], c["end_ts"], c["minutes"], c["soc_start"],
              c["soc_end"], c["kwh_added"], c["miles_added"], c.get("avg_kw"),
              c.get("charge_type"), c.get("lat"), c.get("lon"),
              c.get("label")) for c in (charges or [])],
        )
        # Restore preserved user costs onto the re-derived charges.
        if _costs:
            con.executemany(
                "UPDATE charges SET user_cost_per_kwh = ? WHERE start_ts = ?",
                [(v, k) for k, v in _costs.items()])
        # Rivian prices outlive start_ts shifts that orphan a restored cost.
        apply_rivian_sessions(con)
        con.executemany(
            "INSERT INTO errands (day, drives, miles, kwh_used)"
            " VALUES (?,?,?,?)",
            [(e["day"], e["drives"], e["miles"], e["kwh_used"])
             for e in (errands or [])],
        )
