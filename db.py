"""SQLite storage for snapshots, trips, and idle-drain events."""
import sqlite3
import time
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
    cloud_sync_ts INTEGER,
    drive_mode TEXT
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
    temp_f REAL,
    drive_mode TEXT
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
    wake_count INTEGER,
    anchor_start_ts INTEGER,
    anchor_end_ts INTEGER,
    pct_per_day REAL,
    plugged_in INTEGER,
    approx INTEGER,
    measured INTEGER,
    open_span INTEGER
);

CREATE TABLE IF NOT EXISTS places(
    lat_r INTEGER NOT NULL,
    lon_r INTEGER NOT NULL,
    label TEXT,
    ts INTEGER,
    PRIMARY KEY (lat_r, lon_r)
);

-- Every completed charging session from Rivian's charging API. Paid ones
-- price the DC charges they overlap, resolved in charge_price_info; ones
-- that overlap no charge of ours are sessions we never tracked, counted at
-- report time.
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

-- Manually entered charging prices, kept apart from the derived charges
-- table on purpose: rebuilds delete and recreate charge sessions (their
-- numeric ids are unstable), so a price stored on the session row would
-- be lost whenever a session's start moves or the session disappears.
-- Annotations are keyed by start_ts; clearing a price deletes its row,
-- so 0.00 stays unambiguously a price and never a missing value.
CREATE TABLE IF NOT EXISTS charge_prices(
    start_ts INTEGER PRIMARY KEY,
    user_cost_per_kwh REAL NOT NULL,
    updated_ts INTEGER NOT NULL
);

-- One-row-per-migration markers, so one-time migrations run exactly
-- once even across restarts.
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.row_factory = sqlite3.Row
        yield con
    except BaseException:
        # Roll back explicitly. The previous shape relied on close()
        # discarding the open transaction, which is implicit and easy
        # to break by accident; a failed block must never commit
        # partial work, so say so here.
        con.rollback()
        raise
    else:
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
                    "steady_hours", "wake_count", "anchor_start_ts",
                    "anchor_end_ts", "pct_per_day", "plugged_in",
                    "approx", "measured", "open_span"):
            if col not in cols:
                con.execute(f"ALTER TABLE drains ADD COLUMN {col} REAL")
        snap_cols = {r["name"] for r in
                     con.execute("PRAGMA table_info(snapshots)").fetchall()}
        if "speed" not in snap_cols:
            con.execute("ALTER TABLE snapshots ADD COLUMN speed REAL")
        for col in ("cloud_online INTEGER", "cloud_sync_ts INTEGER",
                    "drive_mode TEXT"):
            name = col.split()[0]
            if name not in snap_cols:
                con.execute(f"ALTER TABLE snapshots ADD COLUMN {col}")
        trip_cols = {r["name"] for r in
                     con.execute("PRAGMA table_info(trips)").fetchall()}
        if "bracketed" not in trip_cols:
            con.execute(
                "ALTER TABLE trips ADD COLUMN bracketed INTEGER DEFAULT 0")
        for col in ("via_lat REAL", "via_lon REAL", "via_label TEXT",
                    "path TEXT", "avg_mph REAL", "temp_f REAL",
                    "drive_mode TEXT"):
            name = col.split()[0]
            if name not in trip_cols:
                con.execute(f"ALTER TABLE trips ADD COLUMN {col}")
        charge_cols = {r["name"] for r in
                       con.execute("PRAGMA table_info(charges)").fetchall()}
        if "user_cost_per_kwh" not in charge_cols:
            con.execute("ALTER TABLE charges ADD COLUMN user_cost_per_kwh REAL")
        # One-time backfill of manually entered charging prices into the
        # annotation table. The marker row commits in the same
        # transaction as the copied rows, so a crash or restart can
        # neither half-apply the backfill nor run it twice: a later
        # clear (which deletes the annotation row) is never resurrected,
        # because this block never runs again once the marker is set.
        marker = con.execute(
            "SELECT value FROM meta WHERE key = 'charge_prices_backfill'"
        ).fetchone()
        if marker is None or marker[0] != "1":
            # Before charge_prices existed, prices pulled from Rivian were
            # written onto charges too. Drop those so the backfill below
            # doesn't freeze them as manual entries.
            for cid, (_, rate) in _rivian_rates(con).items():
                con.execute(
                    "UPDATE charges SET user_cost_per_kwh = NULL"
                    " WHERE id = ? AND abs(user_cost_per_kwh - ?) < 1e-9",
                    (cid, rate))
            con.execute(
                "INSERT OR IGNORE INTO charge_prices"
                " (start_ts, user_cost_per_kwh, updated_ts)"
                " SELECT start_ts, user_cost_per_kwh, ? FROM charges"
                " WHERE user_cost_per_kwh IS NOT NULL ORDER BY id",
                (int(time.time()),))
            con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES"
                " ('charge_prices_backfill', '1')")


def charge_price_info(con, starts):
    """Resolve charging-price annotations for a set of session starts.

    Returns (prices, orphans, ambiguous):
    - prices: {start_ts: cost} for starts identifying exactly one
      session. This is the single resolution point shared by the
      dashboard, the savings totals, and the CSV export, so they
      can never disagree.
    - orphans: [(start_ts, cost)] annotations with no current session.
    - ambiguous: {start_ts} starts shared by 2+ sessions. Their price
      is withheld from every display and total, so one price can never
      be applied to multiple sessions or double-counted.
    """
    from collections import Counter
    counts = Counter(starts)
    ambiguous = {s for s, c in counts.items() if c > 1}
    prices, orphans = {}, []
    startset = set(starts)
    for s, cost in con.execute(
            "SELECT start_ts, user_cost_per_kwh FROM charge_prices"):
        if s not in startset:
            orphans.append((s, cost))
        elif s not in ambiguous:
            prices[s] = cost
    # What Rivian billed fills in behind manual prices. It is recomputed
    # from rivian_sessions on every call, so it follows start_ts shifts and
    # never shows up as an orphan.
    for s, rate in _rivian_rates(con).values():
        if s in startset and s not in ambiguous:
            prices.setdefault(s, rate)
    return prices, orphans, ambiguous


def apply_charge_price(con, start_ts, cost):
    """Apply (or clear, when cost is None) a price for one session.

    Identity is start_ts only. The numeric charge id is unstable
    across re-derives and is never trusted, not even as a first
    lookup. Returns "ok", "not_found", or "ambiguous". The caller
    owns commit/rollback.
    """
    n = con.execute(
        "SELECT COUNT(*) FROM charges WHERE start_ts = ?",
        (start_ts,)).fetchone()[0]
    if n == 0:
        return "not_found"
    if n > 1:
        return "ambiguous"
    if cost is None:
        con.execute("DELETE FROM charge_prices WHERE start_ts = ?",
                    (start_ts,))
    else:
        con.execute(
            "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
            " updated_ts) VALUES (?, ?, ?)"
            " ON CONFLICT(start_ts) DO UPDATE SET"
            " user_cost_per_kwh = excluded.user_cost_per_kwh,"
            " updated_ts = excluded.updated_ts",
            (start_ts, cost, int(time.time())))
    return "ok"


def reassign_charge_price(con, from_ts, to_ts, action):
    """Move or remove an orphaned annotation. Returns a status string:
    "ok", "orphan_not_found", "source_attached", "target_not_found",
    "target_ambiguous", "target_priced", "same_session", or "bad_action".

    Nothing is matched by guesswork: both endpoints are validated
    exactly, and a price is never moved onto a session that already
    has one. The source must still be orphaned (zero matching charge
    rows): a stale form submitted after the session reappeared must
    neither move nor delete the price that is attached again. The
    caller owns commit/rollback.
    """
    if action not in ("move", "remove"):
        return "bad_action"
    row = con.execute(
        "SELECT user_cost_per_kwh FROM charge_prices WHERE start_ts = ?",
        (from_ts,)).fetchone()
    if row is None:
        return "orphan_not_found"
    if con.execute(
            "SELECT COUNT(*) FROM charges WHERE start_ts = ?",
            (from_ts,)).fetchone()[0] > 0:
        return "source_attached"
    if action == "remove":
        con.execute("DELETE FROM charge_prices WHERE start_ts = ?",
                    (from_ts,))
        return "ok"
    if from_ts == to_ts:
        return "same_session"
    n = con.execute(
        "SELECT COUNT(*) FROM charges WHERE start_ts = ?",
        (to_ts,)).fetchone()[0]
    if n == 0:
        return "target_not_found"
    if n > 1:
        return "target_ambiguous"
    if con.execute(
            "SELECT 1 FROM charge_prices WHERE start_ts = ?",
            (to_ts,)).fetchone() is not None:
        return "target_priced"
    con.execute("DELETE FROM charge_prices WHERE start_ts = ?",
                (from_ts,))
    con.execute(
        "INSERT INTO charge_prices (start_ts, user_cost_per_kwh,"
        " updated_ts) VALUES (?, ?, ?)",
        (to_ts, row[0], int(time.time())))
    return "ok"


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
                cloud_online, cloud_sync_ts, drive_mode)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["ts"], s.get("soc"), s.get("capacity_kwh"), s.get("range_mi"),
             s.get("charger_state"), s.get("charge_port"), s.get("power_kw"),
             s.get("power_state"), s.get("gear"),
             s.get("lat"), s.get("lon"), s.get("speed"), s.get("geo_label"),
             s.get("odometer"), s.get("cloud_online"), s.get("cloud_sync_ts"),
             s.get("drive_mode")),
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


def _rivian_rates(con) -> dict[int, tuple[int, float]]:
    """{charge id: (start_ts, $/kWh)} for DC charges Rivian billed.

    Each paid session prices the DC charge it overlaps most in time, one to
    one, and matching runs against every DC charge so a session can't drift
    onto a neighbor of a manually priced one. The rate uses our battery-side
    kWh, not the billed kWh, so the session's EV cost equals what was paid.
    """
    paid = con.execute(
        "SELECT start_ts, end_ts, paid_total FROM rivian_sessions"
        " WHERE paid_total IS NOT NULL AND currency = 'USD'").fetchall()
    charges = con.execute(
        "SELECT id, start_ts, end_ts, kwh_added"
        " FROM charges WHERE lower(charge_type) LIKE '%dc%'").fetchall()
    pairs = []
    for ri, (p_start, p_end, _) in enumerate(paid):
        for c in charges:
            overlap = min(p_end, c[2]) - max(p_start, c[1])
            if overlap > 0:
                pairs.append((overlap, ri, c))
    used_paid, matched = set(), {}
    for _, ri, (cid, start_ts, _, kwh) in sorted(
            pairs, key=lambda p: p[0], reverse=True):
        if ri in used_paid or cid in matched:
            continue
        used_paid.add(ri)
        matched[cid] = (start_ts, paid[ri][2], kwh or 0)
    return {cid: (ts, total / kwh)
            for cid, (ts, total, kwh) in matched.items() if kwh > 0}


def latest_unpriced_dc_end(since: int) -> int | None:
    """End time of the newest unpriced DC charge that ended after ``since``."""
    with connect() as con:
        rows = con.execute(
            "SELECT start_ts, end_ts, charge_type, kwh_added FROM charges"
            ).fetchall()
        prices, _, _ = charge_price_info(con, [r["start_ts"] for r in rows])
    return max((r["end_ts"] for r in rows
                if "dc" in (r["charge_type"] or "").lower()
                and (r["kwh_added"] or 0) > 0 and r["end_ts"] > since
                and r["start_ts"] not in prices), default=None)


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
        # Manually entered prices live in charge_prices, not on the
        # derived rows, so this wipe-and-reinsert cannot lose them.
        # The old start_ts-keyed preserve/restore is gone on purpose:
        # it was the mechanism that dropped prices whenever a
        # session's start moved or the session disappeared.
        con.execute("DELETE FROM trips")
        con.execute("DELETE FROM drains")
        con.execute("DELETE FROM charges")
        con.execute("DELETE FROM errands")
        con.executemany(
            "INSERT INTO trips (start_ts, end_ts, miles, kwh_used, wh_per_mi,"
            " avg_mph, start_lat, start_lon, end_lat, end_lon, start_label,"
            " end_label, bracketed, via_lat, via_lon, via_label, path,"
            " temp_f, drive_mode)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(t["start_ts"], t["end_ts"], t["miles"], t["kwh_used"],
              t["wh_per_mi"], t.get("avg_mph"),
              t.get("start_lat"), t.get("start_lon"),
              t.get("end_lat"), t.get("end_lon"),
              t.get("start_label"), t.get("end_label"),
              1 if t.get("bracketed") else 0,
              t.get("via_lat"), t.get("via_lon"), t.get("via_label"),
              t.get("path"), t.get("temp_f"), t.get("drive_mode"))
             for t in trips],
        )
        con.executemany(
            "INSERT INTO drains (sleep_ts, wake_ts, hours, soc_start, soc_end,"
            " kwh_lost, watts_avg, cd_kwh, cd_watts, steady_kwh, steady_watts,"
            " steady_hours, range_start, range_end, wake_count,"
            " anchor_start_ts, anchor_end_ts, pct_per_day, plugged_in,"
            " approx, measured, open_span)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(d["sleep_ts"], d["wake_ts"], d["hours"], d["soc_start"],
              d["soc_end"], d["kwh_lost"], d["watts_avg"], d.get("cd_kwh"),
              d.get("cd_watts"), d.get("steady_kwh"), d.get("steady_watts"),
              d.get("steady_hours"),
              d.get("range_start"), d.get("range_end"),
              d.get("wake_count", 0), d.get("anchor_start_ts"),
              d.get("anchor_end_ts"), d.get("pct_per_day"),
              d.get("plugged_in", 0), d.get("approx", 0),
              d.get("measured", 0), d.get("open_span", 0)) for d in drains],
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
        con.executemany(
            "INSERT INTO errands (day, drives, miles, kwh_used)"
            " VALUES (?,?,?,?)",
            [(e["day"], e["drives"], e["miles"], e["kwh_used"])
             for e in (errands or [])],
        )
