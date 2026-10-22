"""Wattson API: charging-cost entry, history export, and dashboard login.

Run alongside the dashboard (see docker-compose.yml). The dashboard itself
is static HTML; this small Flask app lets users export sessions and record what
they paid per kWh at public DC fast chargers, which feeds the fuel-savings
math on the next dashboard regeneration. When WATTSON_DASHBOARD_TOKEN is
set it also serves the login page and the check nginx runs before every
dashboard request (see dashboard_auth.py).
"""
import os
import csv
from contextlib import closing
from datetime import datetime, timezone
import io
from flask import Flask, request, redirect, jsonify, Response
import sqlite3
import math
import time

from config import DB_PATH
from dashboard_auth import (SESSION_COOKIE, DashboardAuth, client_ip, is_https,
                            token_from_env)

app = Flask(__name__)

WATTSON_API_HOST = os.environ.get("WATTSON_API_HOST", "127.0.0.1")

auth = DashboardAuth(token_from_env(), time.time)

LOGIN_ERRORS = {
    "bad": "Wrong password.",
    "throttled": "Too many failed attempts. Try again in a few minutes.",
}


def _err_page(msg, code):
    """Friendly error page instead of a bare string on API failures."""
    return (
        '<html><head><meta name="viewport" content="width=device-width,'
        ' initial-scale=1"></head>'
        '<body style="background:#0b0d10;color:#fff;font-family:sans-serif;'
        'padding:40px;max-width:480px;margin:0 auto">'
        f"<p>{msg}</p>"
        '<p><a href="/#charging" style="color:#7ed321">Back to dashboard</a></p>'
        "</body></html>", code)


def _csv_cell(value):
    # Charger names are external text, not spreadsheet formulas.
    if isinstance(value, str) and (
            value.startswith(("\t", "\r", "\n"))
            or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


@app.route("/api/charges.csv")
def export_charges():
    if not auth.authorized(request.headers.get("Authorization"),
                           request.cookies.get(SESSION_COOKIE)):
        return "", 401
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "start_utc", "end_utc", "charge_type", "label", "minutes",
        "kwh_added", "miles_added", "avg_kw", "soc_start", "soc_end",
        "user_cost_per_kwh",
    ])
    try:
        with closing(sqlite3.connect(
                DB_PATH.resolve().as_uri() + "?mode=ro",
                uri=True, timeout=30)) as con:
            for row in con.execute(
                    "SELECT start_ts, end_ts, charge_type, label, minutes,"
                    " kwh_added, miles_added, avg_kw, soc_start, soc_end,"
                    " user_cost_per_kwh FROM charges ORDER BY start_ts"):
                writer.writerow([
                    datetime.fromtimestamp(ts, timezone.utc).isoformat()
                    for ts in row[:2]
                ] + [_csv_cell(value) for value in row[2:]])
    except sqlite3.Error:
        app.logger.exception("Charging CSV export failed")
        return _err_page("Charging history is unavailable. Please try again.",
                         503)
    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        'attachment; filename="wattson-charges.csv"')
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/charge-cost-form", methods=["POST"])
def set_charge_cost_form():
    charge_id = request.form.get("charge_id")
    start_ts = request.form.get("start_ts")
    raw_cost = request.form.get("cost_per_kwh")
    try:
        cid = int(charge_id)
    except (ValueError, TypeError):
        return _err_page("Invalid charge id.", 400)
    # Empty cost clears a previously entered rate (back to unpriced).
    if raw_cost is None or str(raw_cost).strip() == "":
        cost = None
    else:
        try:
            cost = float(raw_cost)
        except (ValueError, TypeError):
            return _err_page("Enter a valid price per kWh.", 400)
        # Validate: reject NaN/Inf, allow 0.00 (free charging) through 10.00.
        if math.isnan(cost) or math.isinf(cost) or not (0.0 <= cost <= 10.0):
            return _err_page("Enter a price between 0 and 10 $/kWh.", 400)
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            cur = con.execute(
                "UPDATE charges SET user_cost_per_kwh = ? WHERE id = ?",
                (cost, cid))
            if cur.rowcount == 0 and start_ts:
                # The dashboard HTML may predate a background re-derive,
                # which reassigns charge IDs. Fall back to the session's
                # start timestamp, which is stable across re-derives.
                try:
                    sts = int(start_ts)
                except (ValueError, TypeError):
                    sts = None
                if sts is not None:
                    cur = con.execute(
                        "UPDATE charges SET user_cost_per_kwh = ?"
                        " WHERE start_ts = ?",
                        (cost, sts))
            if cur.rowcount == 0:
                return _err_page("Charge not found. The charging log may have"
                                 " refreshed; please reload the page and try"
                                 " again.", 404)
            con.commit()
    except sqlite3.OperationalError as e:
        app.logger.error("DB error: %s", e)
        return _err_page("Database is busy. Please try again in a moment.",
                         503)
    # Regenerate the dashboard so the new cost shows immediately.
    try:
        import report
        report.generate()
    except Exception:
        app.logger.exception("Dashboard regeneration failed")
    return redirect("/#charging", code=303)


def _login_page(error, code):
    message = LOGIN_ERRORS.get(error)
    err = f'<p style="color:#ff6b6b">{message}</p>' if message else ""
    return (
        '<html><head><meta name="viewport" content="width=device-width,'
        ' initial-scale=1"><title>Wattson login</title></head>'
        '<body style="background:#0b0d10;color:#fff;font-family:sans-serif;'
        'padding:40px;max-width:320px;margin:0 auto">'
        '<form method="post" action="/login"'
        ' style="display:flex;flex-direction:column;gap:12px">'
        '<h1 style="margin:0">Wattson</h1>'
        '<p style="margin:0;color:#9aa0a6">This dashboard is private.</p>'
        f"{err}"
        '<input type="password" name="password" placeholder="password"'
        ' autocomplete="current-password" autofocus'
        ' style="font-size:15px;padding:10px 12px;border:1px solid #2a2f36;'
        'border-radius:4px;background:#14181d;color:#fff">'
        '<button type="submit" style="font-size:15px;padding:10px;border:0;'
        'border-radius:4px;background:#7ed321;color:#0b0d10;'
        'font-weight:600">Sign in</button>'
        "</form></body></html>", code)


@app.route("/api/auth/check")
def auth_check():
    # nginx auth_request treats any status other than 2xx/401/403 as a
    # server error for the whole site, so this only ever says 200 or 401.
    if auth.authorized(request.headers.get("Authorization"),
                       request.cookies.get(SESSION_COOKIE)):
        return "", 200
    return "", 401


@app.route("/login", methods=["GET"])
def login_form():
    return _login_page(request.args.get("error"), 200)


@app.route("/login", methods=["POST"])
def login():
    ip = client_ip(request.headers, request.remote_addr)
    result = auth.login(ip, request.form.get("password", ""))
    if result == "bad":
        app.logger.warning("dashboard login failed from %s", ip)
        return _login_page("bad", 401)
    if result == "throttled":
        return _login_page("throttled", 429)
    # Always "/", never a caller-supplied target, so this is no open redirect.
    resp = redirect("/", code=303)
    if result == "ok":
        resp.headers["Set-Cookie"] = auth.session_cookie(
            is_https(request.headers))
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = redirect("/login", code=303)
    resp.headers["Set-Cookie"] = auth.clear_cookie(is_https(request.headers))
    return resp


@app.route("/api/health")
def health():
    return jsonify({"ok": True})



# ---------------------------------------------------------------------------
# Send destination navigation (see nav.py). nginx gates /api/ when a
# dashboard token is set; these routes also check in-app (like the CSV
# export above) because they act on the car, and the API port should not
# be reachable without the token just by bypassing nginx.
# ---------------------------------------------------------------------------

@app.before_request
def _nav_gate():
    if request.path.startswith("/api/nav/"):
        if not auth.authorized(request.headers.get("Authorization"),
                               request.cookies.get(SESSION_COOKIE)):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401


_saved_cache = {"ts": 0.0, "places": []}


def _car_state():
    """Latest snapshot + recent efficiency from the tracker DB."""
    car = {}
    wh_mi = None
    try:
        with sqlite3.connect(str(DB_PATH), timeout=30) as con:
            row = con.execute(
                "SELECT ts, soc, range_mi, lat, lon FROM snapshots"
                " ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                car = {"ts": row[0], "soc": row[1], "range_mi": row[2],
                       "lat": row[3], "lon": row[4]}
            agg = con.execute(
                "SELECT SUM(kwh_used), SUM(miles) FROM trips"
                " WHERE miles >= 1 AND wh_per_mi BETWEEN 100 AND 900"
                " AND start_ts > strftime('%s', 'now', '-90 days')").fetchone()
            if agg and agg[0] and agg[1]:
                wh_mi = agg[0] * 1000.0 / agg[1]
            else:
                agg = con.execute(
                    "SELECT SUM(kwh_used), SUM(miles) FROM trips"
                    " WHERE miles >= 1 AND wh_per_mi BETWEEN 100 AND 900"
                ).fetchone()
                if agg and agg[0] and agg[1]:
                    wh_mi = agg[0] * 1000.0 / agg[1]
    except sqlite3.Error:
        app.logger.exception("nav car state failed")
    return car, (round(wh_mi) if wh_mi else 380)


def _saved_places_cached():
    import time as _time
    import nav
    if _time.time() - _saved_cache["ts"] < 300 and _saved_cache["places"]:
        return _saved_cache["places"], True
    try:
        places = nav.saved_places()
        _saved_cache.update(ts=_time.time(), places=places)
        return places, True
    except Exception:
        app.logger.exception("saved places fetch failed")
        return _saved_cache["places"], False


def _home_place(places):
    for p in places:
        if p.get("type") == "FAVORITE_HOME":
            return p
    try:
        from config import HOME_LAT, HOME_LON
        if HOME_LAT == HOME_LAT and HOME_LON == HOME_LON:  # not NaN
            return {"name": "Home", "address": "", "lat": HOME_LAT,
                    "lon": HOME_LON, "place_id": None, "type": "HOME",
                    "url": None}
    except Exception:
        pass
    return None


def _clean_place(data):
    """Validate a place dict from the dashboard. Returns (place, error)."""
    if not isinstance(data, dict):
        return None, "Missing place"
    url = data.get("url")
    if url:
        if not (isinstance(url, str) and url.startswith("https://")
                and ("google." in url or "telenav." in url
                     or "goo.gl" in url)):
            return None, "That link is not a map link"
        return {"name": (str(data.get("name") or "Map link"))[:120],
                "address": "", "lat": None, "lon": None,
                "place_id": None, "type": "", "url": url}, None
    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
    except (TypeError, ValueError):
        return None, "That place has no coordinates"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "That place has no coordinates"
    pid = data.get("place_id")
    return {"name": (str(data.get("name") or "Destination"))[:120],
            "address": (str(data.get("address") or ""))[:240],
            "lat": lat, "lon": lon,
            "place_id": str(pid)[:80] if pid else None,
            "type": str(data.get("type") or ""), "url": None}, None


@app.route("/api/nav/state")
def nav_state():
    import nav
    car, wh_mi = _car_state()
    places, fresh = _saved_places_cached()
    out = []
    for p in places:
        q = dict(p)
        if car.get("lat") is not None and p.get("lat") is not None:
            q["distance_mi"] = round(
                nav.haversine_mi(car["lat"], car["lon"], p["lat"], p["lon"]), 1)
        out.append(q)
    return jsonify({"ok": True, "car": car, "wh_mi": wh_mi,
                    "places": out, "places_fresh": fresh})


@app.route("/api/nav/search")
def nav_search():
    import nav
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "results": []})
    car, _wh = _car_state()
    try:
        results = nav.search_places(q, car.get("lat"), car.get("lon"))
    except Exception:
        app.logger.exception("place search failed")
        return jsonify({"ok": False, "error": "Search failed. Try again."}), 502
    for r in results:
        if car.get("lat") is not None:
            r["distance_mi"] = round(
                nav.haversine_mi(car["lat"], car["lon"], r["lat"], r["lon"]), 1)
    results.sort(key=lambda r: r.get("distance_mi", 1e9))
    return jsonify({"ok": True, "results": results})


@app.route("/api/nav/plan", methods=["POST"])
def nav_plan():
    import nav
    data = request.get_json(force=True, silent=True) or {}
    place, err = _clean_place(data.get("place"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if place.get("lat") is None:
        return jsonify({"ok": False,
                        "error": "Planning needs a place, not a link"}), 400
    car, _wh = _car_state()
    if car.get("lat") is None or car.get("soc") is None:
        return jsonify({"ok": False, "error": "No recent car data"}), 503
    places, _fresh = _saved_places_cached()
    home = _home_place(places)
    try:
        result = nav.plan(place, bool(data.get("round_trip")), home,
                          {"lat": car["lat"], "lon": car["lon"]}, car["soc"])
    except Exception:
        app.logger.exception("trip plan failed")
        return jsonify({"ok": False,
                        "error": "Rivian's planner did not answer."}), 502
    return jsonify(result)


@app.route("/api/nav/send", methods=["POST"])
def nav_send():
    import nav
    data = request.get_json(force=True, silent=True) or {}
    place, err = _clean_place(data.get("place"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    mode = "trip" if data.get("mode") == "trip" else "direct"
    if mode == "trip" and place.get("lat") is None:
        return jsonify({"ok": False,
                        "error": "Trips need a place, not a link"}), 400
    car, _wh = _car_state()
    if car.get("lat") is None:
        return jsonify({"ok": False, "error": "No recent car data"}), 503
    places, _fresh = _saved_places_cached()
    home = _home_place(places)
    job_id = nav.start_send_job(place, mode, bool(data.get("round_trip")),
                                home, {"lat": car["lat"], "lon": car["lon"]})
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/nav/status")
def nav_status():
    import nav
    job = nav.job_status(request.args.get("job") or "")
    if job is None:
        return jsonify({"ok": False, "error": "Unknown job"}), 404
    return jsonify({"ok": True, "job": job})


@app.route("/api/nav/map")
def nav_map():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return "Bad coordinates", 400
    try:
        import report
        img = report._render_point_map(lat, lon)
    except Exception:
        app.logger.exception("nav map render failed")
        img = None
    if img is None:
        return "Map unavailable", 404
    response = Response(img, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


if __name__ == "__main__":
    app.run(host=WATTSON_API_HOST, port=5000, threaded=True)
