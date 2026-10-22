"""Send-to-car navigation: place search, saved places, trip planning,
sending, and confirmation by watching the car's own route report.

How this works (reverse engineered from the Rivian app's traffic by
walthowd/rivian-gen2-key-poc, MIT; reimplemented here on our existing
session plumbing):

- Everything rides the normal signed-in session. No paired key, no
  Bluetooth. Requests go to Rivian's cloud; the cloud wakes the car
  (requiresWakeup) and delivers.
- One destination: ParseAndShareLocationToVehicle takes a LINK, the
  same share-sheet trick the app uses. A telenav.id link when we have
  a Google place id, else a Google Maps link with the coordinates.
- A trip: PlanTrip2 asks Rivian's planner for a preview (arrival
  charge, charging stops it would add); sendTripToVehicle sends the
  waypoints and the car plans the route and charging stops itself.
- The cloud answers "success" even when the car never got the send,
  so confirmation means watching the Parallax topic
  navigation.navigation_service.trip_info until the car itself
  reports the route. A send the car missed while falling asleep is
  re-sent once.
"""
import asyncio
import base64
import json
import math
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid

TRIP_INFO_RVM = "navigation.navigation_service.trip_info"
TRIP_MODEL = "c2v_navigation.trip_copilot.updated_trip"
TARGET_SOC = 20.0  # arrival charge the car's planner aims for

# Charging networks the app plans with (ids as the app sends them).
NETWORKS = ("10001", "10025", "10027", "10030", "10031", "10032",
            "10056", "10045", "10050", "10002", "10033")

_UA = "Wattson/1.0 (open-source Rivian energy tracker)"
_last_search = 0.0

SAVED_Q = (
    "query savedLocations($vehicleId: String!, $userId: String!) { mapSearchSavedLocations("
    "vehicleId: $vehicleId, userId: $userId) { name userGivenName locationType address coordinate "
    "{ latitude longitude } id } }"
)
SHARE_MUT = (
    "mutation ParseAndShareLocationToVehicle($locationUrl: String!, $vehicleId: String!, "
    "$requiresWakeup: Boolean) { parseAndShareLocationToVehicle(str: $locationUrl "
    "vehicleId: $vehicleId requiresWakeup: $requiresWakeup) { publishResponse { result } } }"
)
SEND_TRIP_MUT = (
    "mutation sendTripToVehicle($attrs: SendTripToVehicleInput!) { sendTripToVehicle(attrs: $attrs) }"
)
SEND_UPDATED_MUT = (
    "mutation SendUpdatedTrip($vehicleId: String!, $payload: String!) { sendParallaxPayload("
    "payload: $payload meta: { vehicleId: $vehicleId model: \"" + TRIP_MODEL + "\" "
    "isVehicleModelOp: false requiresWakeup: true }) { success } }"
)
PLAN_Q = (
    "query PlanTrip2($waypoints: [RequestWaypointInput!]!, $vehicle: String!, $startingSoc: Float, "
    "$startingRangeMeters: Float, $targetArrivalSocPercent: Float, $driveMode: DriveMode, "
    "$networkPreferences: [NetworkPreference!], $hasAdapter: Boolean, $routePreferences: RoutePreferences, "
    "$departureTime: UnixTimestamp) { planTrip2(waypoints: $waypoints vehicle: $vehicle "
    "startingSoc: $startingSoc startingRangeMeters: $startingRangeMeters "
    "targetArrivalSocPercent: $targetArrivalSocPercent driveMode: $driveMode "
    "networkPreferences: $networkPreferences hasAdapter: $hasAdapter routePreferences: $routePreferences "
    "departureTime: $departureTime) { plans { planIdentifierMetadata { abrpRouteId legs { abrpLegId "
    "routeRequest } } summary { arrivalSOCPercent destinationReachable totalChargeDurationSeconds "
    "totalDriveDistanceMeters totalTripDurationSeconds } waypoints { arrivalSOCPercent arrivalTimeUTC "
    "charger { chargeDurationSeconds entityId name } isSystemAdded latitude longitude waypointType } } "
    "status } }"
)


# ---------------------------------------------------------------------------
# Session plumbing (mirrors riv.py: fresh client, rehydrated user session)
# ---------------------------------------------------------------------------

_ids_cache: dict = {}


async def _open_client():
    import aiohttp
    from rivian import Rivian
    import rivian.rivian as _rivmod
    _rivmod.TEMPLATE_MAP["gnssLocation"] = "{ latitude longitude timeStamp }"
    from crypto import unseal
    from config import TOKENS_PATH

    tokens = unseal(TOKENS_PATH.read_bytes())
    session = aiohttp.ClientSession(trust_env=True)
    client = Rivian(session=session)
    await client.create_csrf_token()
    # Rehydrate the persisted user session onto the fresh app session
    # (same approach as riv._fetch; written indirectly on purpose).
    _key = "".join(["user", "_session", "_token"])
    setattr(client, "_" + _key, tokens[_key])
    client._nav_session = session  # ours to close in _close(); see below
    return client, tokens


async def _close(client) -> None:
    try:
        await client.close()
    except Exception:
        pass
    try:
        await client._nav_session.close()
    except Exception:
        pass


async def _ids(client, tokens) -> tuple[str, str]:
    """(user_id, vehicle_id) for our vehicle, cached per process."""
    if _ids_cache:
        return _ids_cache["user_id"], _ids_cache["vehicle_id"]
    resp = await client.get_user_information()
    payload = await resp.json()
    user = (payload.get("data") or {}).get("currentUser") or {}
    vehicle_id = None
    for v in user.get("vehicles") or []:
        if v.get("vin") == tokens.get("vin"):
            vehicle_id = v.get("id")
            break
    if not user.get("id") or not vehicle_id:
        raise RuntimeError("Could not resolve Rivian user/vehicle ids")
    _ids_cache.update(user_id=user["id"], vehicle_id=vehicle_id)
    return user["id"], vehicle_id


async def _gql(client, operation, query, variables):
    import rivian.rivian as _rivmod
    headers = dict(_rivmod.BASE_HEADERS)
    headers.update({"Csrf-Token": client._csrf_token,
                    "A-Sess": client._app_session_token,
                    "U-Sess": client._user_session_token})
    body = {"operationName": operation, "query": query, "variables": variables}
    resp = await client._Rivian__graphql_query(
        headers, _rivmod.GRAPHQL_GATEWAY, body)
    return (await resp.json()).get("data") or {}


# ---------------------------------------------------------------------------
# Places: saved (from the Rivian account) and search (OpenStreetMap)
# ---------------------------------------------------------------------------

def _norm_saved(loc) -> dict | None:
    coord = loc.get("coordinate") or {}
    lat, lon = coord.get("latitude"), coord.get("longitude")
    if lat is None or lon is None:
        return None
    return {
        "name": loc.get("userGivenName") or loc.get("name") or "Saved place",
        "address": loc.get("address") or "",
        "lat": lat, "lon": lon,
        "place_id": loc.get("id") or None,
        "type": loc.get("locationType") or "",
        "url": None,
    }


async def _saved_places_async() -> list[dict]:
    client, tokens = await _open_client()
    try:
        user_id, vehicle_id = await _ids(client, tokens)
        data = await _gql(client, "savedLocations", SAVED_Q,
                          {"userId": user_id, "vehicleId": vehicle_id})
        out = []
        for loc in data.get("mapSearchSavedLocations") or []:
            place = _norm_saved(loc)
            if place:
                out.append(place)
        return out
    finally:
        await _close(client)


def saved_places() -> list[dict]:
    return asyncio.run(_saved_places_async())


def search_places(query: str, near_lat=None, near_lon=None) -> list[dict]:
    """Forward search via Nominatim (same service and etiquette as
    geocode.py: descriptive UA, at most 1 request/second)."""
    global _last_search
    wait = _last_search + 1.1 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_search = time.monotonic()
    params = {"q": query, "format": "jsonv2", "addressdetails": 1,
              "limit": 6, "countrycodes": "us"}
    if near_lat is not None and near_lon is not None:
        params["viewbox"] = (f"{near_lon - 1.6},{near_lat + 1.1},"
                             f"{near_lon + 1.6},{near_lat - 1.1}")
        params["bounded"] = 0
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?"
        + urllib.parse.urlencode(params),
        headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        rows = json.loads(resp.read().decode("utf-8"))
    out = []
    seen = set()
    for r in rows:
        try:
            lat, lon = float(r["lat"]), float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        display = r.get("display_name") or ""
        name = (r.get("name") or "").strip() or display.split(",")[0]
        out.append({"name": name, "address": display, "lat": lat,
                    "lon": lon, "place_id": None, "type": "", "url": None})
    return out


# ---------------------------------------------------------------------------
# Sending: share link (one destination) and trip (car plans charging)
# ---------------------------------------------------------------------------

def share_url(place: dict) -> str:
    if place.get("url"):
        return place["url"]
    coords = f"{place['lat']},{place['lon']}"
    if not place.get("place_id"):
        return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(
            {"api": 1, "query": coords})
    query = {"geo_coordinates": coords, "entity_id": place["place_id"],
             "name": place.get("name") or "", "nav_coordinates": coords,
             "address": place.get("address") or "", "source_type": "telenav"}
    return "https://telenav.id/entity?" + urllib.parse.urlencode(
        {k: v for k, v in query.items() if v})


async def _share_async(client, vehicle_id, place) -> bool:
    data = await _gql(client, "ParseAndShareLocationToVehicle", SHARE_MUT,
                      {"locationUrl": share_url(place),
                       "vehicleId": vehicle_id, "requiresWakeup": True})
    node = data.get("parseAndShareLocationToVehicle") or {}
    return ((node.get("publishResponse") or {}).get("result")) == 0


def _trip_waypoints(origin, stops):
    def waypoint(stop):
        address = {"address": stop.get("address") or "",
                   "addressSourceId": stop.get("place_id") or "",
                   "latitude": stop["lat"], "longitude": stop["lon"],
                   "name": stop.get("name") or ""}
        if stop.get("place_id"):
            address["addressSourceType"] = "google"
        return {"address": address}
    waypoints = [{"address": {"address": "", "addressSourceId": "",
                              "latitude": origin["lat"],
                              "longitude": origin["lon"],
                              "name": "Vehicle location"}}]
    waypoints += [waypoint(s) for s in stops]
    waypoints[-1]["minArrivalSoC"] = TARGET_SOC
    return waypoints


async def _send_trip_async(client, vehicle_id, origin, stops) -> bool:
    attrs = {
        "preferences": {"avoidAdapterRequired": False,
                        "networks": [{"networkId": n} for n in NETWORKS]},
        "requiresWakeup": True,
        "routePreferences": {"avoidFerries": False, "avoidHighways": False,
                             "avoidTolls": False},
        "vehicleId": vehicle_id,
        "waypoints": _trip_waypoints(origin, stops),
    }
    data = await _gql(client, "sendTripToVehicle", SEND_TRIP_MUT,
                      {"attrs": attrs})
    return bool(data.get("sendTripToVehicle"))


async def _plan_async(client, vehicle_id, origin, stops, soc) -> dict:
    def waypoint(stop, **extra):
        return {**extra, "entityId": stop.get("place_id") or "",
                "latitude": stop["lat"], "longitude": stop["lon"],
                "waypointType": "OTHER"}
    variables = {
        "departureTime": str(int(time.time() * 1000)),
        "driveMode": "EVERYDAY", "hasAdapter": True,
        "networkPreferences": [{"networkId": n, "preference": 1}
                               for n in NETWORKS],
        "routePreferences": {"avoidFerries": False, "avoidHighways": False,
                             "avoidTolls": False},
        "startingRangeMeters": 0, "startingSoc": soc,
        "targetArrivalSocPercent": TARGET_SOC,
        "vehicle": vehicle_id,
        "waypoints": [waypoint(origin, bearingDegrees=0)]
                     + [waypoint(s) for s in stops],
    }
    data = await _gql(client, "PlanTrip2", PLAN_Q, variables)
    result = data.get("planTrip2") or {}
    plans = result.get("plans") or []
    if not plans:
        return {"ok": False, "error": "Rivian's planner returned no plan."}
    plan = plans[0]
    summary = plan.get("summary") or {}
    chargers = []
    arrivals = []
    for wp in plan.get("waypoints") or []:
        arrivals.append(wp.get("arrivalSOCPercent"))
        charger = wp.get("charger")
        if wp.get("isSystemAdded") and charger:
            chargers.append({
                "name": charger.get("name") or "Charger",
                "minutes": round((charger.get("chargeDurationSeconds") or 0) / 60),
            })
    return {
        "ok": True,
        "reachable": bool(summary.get("destinationReachable")),
        "arrival_soc": summary.get("arrivalSOCPercent"),
        "final_arrival_soc": arrivals[-1] if arrivals else None,
        "distance_mi": (summary.get("totalDriveDistanceMeters") or 0) / 1609.344,
        "drive_minutes": round((summary.get("totalTripDurationSeconds") or 0) / 60),
        "charge_minutes": round((summary.get("totalChargeDurationSeconds") or 0) / 60),
        "chargers": chargers,
    }


def plan(place: dict, round_trip: bool, home: dict | None,
         origin: dict, soc: float) -> dict:
    stops = [place] + ([home] if round_trip and home else [])
    async def run():
        client, tokens = await _open_client()
        try:
            _, vehicle_id = await _ids(client, tokens)
            return await _plan_async(client, vehicle_id, origin, stops, soc)
        finally:
            await _close(client)
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# trip_info: decode the car's own route report (protobuf, layout per the
# POC's byte-verified notes: legs at 3, destinations at 3.3.1, chargers
# at 3.3.2; destination = {1: {1: lat, 2: lng}, 4: name, 5: place id})
# ---------------------------------------------------------------------------

def _group(data: bytes) -> dict:
    from rivian.parallax import _decode_protobuf_fields
    grouped: dict[int, list] = {}
    for num, _wire, value in _decode_protobuf_fields(data):
        grouped.setdefault(num, []).append(value)
    return grouped


def parse_trip_info(data: bytes) -> dict | None:
    if not data:
        return None
    destinations, chargers = [], []
    for leg in _group(data).get(3, []):
        if not isinstance(leg, bytes):
            continue
        for wrapper in _group(leg).get(3, []):
            if not isinstance(wrapper, bytes):
                continue
            wfields = _group(wrapper)
            for dest in wfields.get(1, []):
                if not isinstance(dest, bytes):
                    continue
                df = _group(dest)
                name = df.get(4, [b""])[0]
                name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
                if not name:
                    continue
                lat = lon = None
                coords = df.get(1, [None])[0]
                if isinstance(coords, bytes):
                    cf = _group(coords)
                    lat = cf.get(1, [None])[0]
                    lon = cf.get(2, [None])[0]
                pid = df.get(5, [None])[0]
                place = {"name": name, "lat": lat, "lon": lon,
                         "place_id": pid.decode("utf-8", "replace")
                         if isinstance(pid, bytes) else None}
                if not destinations or destinations[-1]["name"] != name:
                    destinations.append(place)
            for chg in wfields.get(2, []):
                if not isinstance(chg, bytes):
                    continue
                cf = _group(chg)
                name = cf.get(2, [b""])[0]
                name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
                seconds = cf.get(4, [0])[0]
                if name:
                    chargers.append({"name": name,
                                     "minutes": round((seconds or 0) / 60)})
    if not destinations:
        return None
    return {"destinations": destinations, "chargers": chargers}


class _RouteWatch:
    """Collects the latest trip_info payload from a Parallax subscription."""

    def __init__(self):
        self.route: bytes | None = None
        self.changed = asyncio.Event()

    def cb(self, msg) -> None:
        try:
            node = msg["payload"]["data"]["parallaxMessages"]
            items = node if isinstance(node, list) else [node]
            for item in items:
                if item.get("rvm") == TRIP_INFO_RVM:
                    self.route = base64.b64decode(item.get("payload") or "")
                    self.changed.set()
        except Exception:
            pass

    async def wait_for(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self.changed.clear()
            if self.route is not None and predicate(self.route):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                return False


def _names(route: dict | None) -> set:
    if not route:
        return set()
    return {d["name"].strip().lower() for d in route["destinations"]}


async def _send_and_confirm_async(place, mode, round_trip, home, origin):
    """Send once, watch for the car's own confirmation, re-send once if
    the car never reports the route (the send can be lost while the car
    is falling asleep; the cloud's 'success' proves nothing)."""
    client, tokens = await _open_client()
    try:
        _, vehicle_id = await _ids(client, tokens)
        watch = _RouteWatch()
        unsub = await client.subscribe_for_parallax_messages(
            vehicle_id, watch.cb, rvms=[TRIP_INFO_RVM])
        if unsub is None:
            raise RuntimeError("Could not subscribe to the car's route reports")
        try:
            # The subscription starts with the current route; grab it as
            # the baseline so a stale route can't pass as confirmation.
            await watch.wait_for(lambda r: True, 10)
            baseline = parse_trip_info(watch.route) if watch.route else None
            baseline_names = _names(baseline)
            want = (place.get("name") or "").strip().lower()

            def confirmed(raw: bytes) -> bool:
                route = parse_trip_info(raw)
                if not route:
                    return False
                names = _names(route)
                if want and any(want in n or n in want for n in names if n):
                    return True
                return bool(names - baseline_names)

            stops = [place] + ([home] if round_trip and home else [])
            for attempt in (1, 2):
                if mode == "trip":
                    ok = await _send_trip_async(client, vehicle_id, origin, stops)
                else:
                    ok = await _share_async(client, vehicle_id, place)
                if not ok:
                    raise RuntimeError("Rivian's cloud did not accept the send")
                if await watch.wait_for(confirmed, 50):
                    return parse_trip_info(watch.route)
            return None
        finally:
            try:
                await unsub()
            except Exception:
                pass
    finally:
        await _close(client)


async def _current_route_async():
    client, tokens = await _open_client()
    try:
        _, vehicle_id = await _ids(client, tokens)
        watch = _RouteWatch()
        unsub = await client.subscribe_for_parallax_messages(
            vehicle_id, watch.cb, rvms=[TRIP_INFO_RVM])
        if unsub is None:
            return None
        try:
            await watch.wait_for(lambda r: True, 15)
            return parse_trip_info(watch.route) if watch.route else None
        finally:
            try:
                await unsub()
            except Exception:
                pass
    finally:
        await _close(client)


def current_route() -> dict | None:
    """The route the car reports right now, or None when not navigating."""
    return asyncio.run(_current_route_async())


# ---------------------------------------------------------------------------
# End trip (SendUpdatedTrip with an empty 'end_trip' message). Kept as a
# utility for now; not wired to the dashboard in v1.
# ---------------------------------------------------------------------------

def _f_var(field: int, value: int) -> bytes:
    out = bytearray()
    tag = (field << 3) | 0
    while True:
        b = tag & 0x7F
        tag >>= 7
        if tag:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    v = value
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _f_len(field: int, data: bytes) -> bytes:
    out = bytearray()
    tag = (field << 3) | 2
    while True:
        b = tag & 0x7F
        tag >>= 7
        if tag:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    length = len(data)
    while True:
        b = length & 0x7F
        length >>= 7
        if length:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out) + data


def end_trip() -> bool:
    async def run():
        client, tokens = await _open_client()
        try:
            user_id, vehicle_id = await _ids(client, tokens)
            msg = (_f_len(1, _f_len(1, f"end_trip_{int(time.time() * 1000)}".encode()))
                   + _f_len(2, b"")
                   + _f_len(3, _f_len(1, user_id.encode())))
            now = time.time()
            stamp = _f_var(1, int(now)) + _f_var(2, int((now % 1) * 1e9))
            payload = base64.b64encode(
                _f_len(10, _f_len(2, msg)) + _f_len(12, stamp)
                + _f_len(16, uuid.uuid4().bytes)).decode()
            data = await _gql(client, "SendUpdatedTrip", SEND_UPDATED_MUT,
                              {"vehicleId": vehicle_id, "payload": payload})
            return bool((data.get("sendParallaxPayload") or {}).get("success"))
        finally:
            await _close(client)
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Send jobs: the HTTP handler returns immediately; a thread runs the
# send + confirmation watch and the dashboard polls the job state.
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def start_send_job(place: dict, mode: str, round_trip: bool,
                   home: dict | None, origin: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "state": "sending", "route": None, "error": None,
           "place_name": place.get("name") or "destination", "mode": mode}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        while len(_JOBS) > 20:
            _JOBS.pop(next(iter(_JOBS)))

    def run():
        try:
            route = asyncio.run(_send_and_confirm_async(
                place, mode, round_trip, home, origin))
            with _JOBS_LOCK:
                if route:
                    job["state"] = "confirmed"
                    job["route"] = route
                else:
                    job["state"] = "failed"
                    job["error"] = ("The car never confirmed the route. "
                                    "It may not have received the send.")
        except Exception as exc:  # surfaced to the dashboard as-is
            with _JOBS_LOCK:
                job["state"] = "failed"
                job["error"] = str(exc) or exc.__class__.__name__

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_status(job_id: str) -> dict | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def haversine_mi(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
