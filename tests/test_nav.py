"""Send destination nav: gate, POST guards, place validation, helpers.

The nav routes act on the car and read its saved places and live
location, so unlike the rest of the API they require a dashboard
token to be set at all (see api._nav_gate).
"""
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import api
import config
import db
import nav
from dashboard_auth import DashboardAuth

SECRET = "test-secret-token"
AUTH = {"Authorization": f"Bearer {SECRET}"}


class NavGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.db"
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(db.SCHEMA)
        self.client = api.app.test_client()

    def patch_all(self, token):
        for target, value in [
            ("api.auth", DashboardAuth(token, lambda: 1700000000)),
            ("api.DB_PATH", self.database),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_nav_off_until_token_set(self):
        self.patch_all(None)
        r = self.client.get("/api/nav/state")
        self.assertEqual(r.status_code, 403)
        self.assertIn("password", r.get_json()["error"])

    def test_unauthorized_when_token_set_but_no_credentials(self):
        self.patch_all(SECRET)
        r = self.client.get("/api/nav/state")
        self.assertEqual(r.status_code, 401)

    def test_state_ok_with_bearer(self):
        self.patch_all(SECRET)
        r = self.client.get("/api/nav/state", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["usable_kwh"], config.USABLE_KWH)

    def test_send_rejects_non_json_body(self):
        self.patch_all(SECRET)
        r = self.client.post("/api/nav/send", data="place=x",
                             content_type="text/plain", headers=AUTH)
        self.assertEqual(r.status_code, 400)

    def test_send_rejects_cross_origin(self):
        self.patch_all(SECRET)
        r = self.client.post(
            "/api/nav/send", json={"place": {}},
            headers={**AUTH, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_send_conflict_while_another_send_runs(self):
        self.patch_all(SECRET)
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute(
                "INSERT INTO snapshots (ts, soc, range_mi, lat, lon)"
                " VALUES (1700000000, 55.0, 180.0, 34.0, -118.0)")
        job = {"id": "x", "state": "sending", "route": None,
               "error": None, "place_name": "A", "mode": "direct"}
        with mock.patch.dict(nav._JOBS, {"x": job}):
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1, "lon": -118.1}},
                headers=AUTH)
        self.assertEqual(r.status_code, 409)

    def _seed_car_row(self):
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute(
                "INSERT INTO snapshots (ts, soc, range_mi, lat, lon)"
                " VALUES (1700000000, 55.0, 180.0, 34.0, -118.0)")

    def test_round_trip_coerces_direct_to_trip(self):
        self.patch_all(SECRET)
        self._seed_car_row()
        home = {"name": "Home", "address": "", "lat": 34.0,
                "lon": -118.0, "place_id": None,
                "type": "FAVORITE_HOME"}
        with mock.patch("api._saved_places_cached",
                        return_value=([home], True)), \
                mock.patch("nav.start_send_job",
                           return_value="job1") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1,
                                "lon": -118.1},
                      "mode": "direct", "round_trip": True},
                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(start.call_args.args[1], "trip")
        self.assertTrue(start.call_args.args[2])

    def test_direct_send_needs_no_car_position_or_places(self):
        self.patch_all(SECRET)
        # A saved place (with a place id) stays direct. A bare
        # address result without one now upgrades to a trip, so it
        # would need a car position and saved places instead.
        with mock.patch("api._saved_places_cached") as places, \
                mock.patch("nav.start_send_job",
                           return_value="job9") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1,
                                "lon": -118.1,
                                "place_id": "ChIJtest"},
                      "mode": "direct"},
                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        places.assert_not_called()
        self.assertEqual(start.call_args.args[1], "direct")
        self.assertIsNone(start.call_args.args[3])

    def test_round_trip_without_home_is_a_clear_400(self):
        self.patch_all(SECRET)
        self._seed_car_row()
        with mock.patch("api._saved_places_cached",
                        return_value=([], True)), \
                mock.patch("api._home_place", return_value=None):
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1,
                                "lon": -118.1},
                      "round_trip": True},
                headers=AUTH)
        self.assertEqual(r.status_code, 400)

    def test_map_rejects_out_of_range_coordinates(self):
        self.patch_all(SECRET)
        r = self.client.get("/api/nav/map?lat=999&lon=0", headers=AUTH)
        self.assertEqual(r.status_code, 400)

    def test_map_serves_rendered_image(self):
        self.patch_all(SECRET)
        with mock.patch("report._render_point_map",
                        return_value=b"\xff\xd8img"):
            r = self.client.get("/api/nav/map?lat=34.0&lon=-118.0",
                                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")


class CleanPlaceTests(unittest.TestCase):
    def test_google_maps_link_ok(self):
        place, err = api._clean_place(
            {"name": "X", "url": "https://www.google.com/maps/place/X"})
        self.assertIsNone(err)
        self.assertTrue(place["url"].startswith("https://www.google.com/"))

    def test_telenav_and_short_links_ok(self):
        for url in ("https://telenav.id/entity?entity_id=1",
                    "https://goo.gl/maps/abc",
                    "https://maps.app.goo.gl/abc"):
            place, err = api._clean_place({"url": url})
            self.assertIsNone(err, url)

    def test_host_spoofs_rejected(self):
        for url in ("https://google.com.evil.example/x",
                    "https://evil.example/?u=google.com",
                    "https://notgoogle.com/maps",
                    "http://www.google.com/maps"):
            place, err = api._clean_place({"url": url})
            self.assertIsNotNone(err, url)

    def test_bad_coordinates_rejected(self):
        for bad in ({"lat": 999, "lon": 0}, {"lat": "x", "lon": 0}, {}):
            place, err = api._clean_place(bad)
            self.assertIsNotNone(err, bad)

    def test_place_fields_normalized(self):
        place, err = api._clean_place(
            {"name": "B", "address": "1 Main St", "lat": 34.1,
             "lon": -118.1, "place_id": 12345})
        self.assertIsNone(err)
        self.assertEqual(place["place_id"], "12345")
        self.assertEqual(place["lat"], 34.1)


class HomePlaceTests(unittest.TestCase):
    def test_favorite_home_wins(self):
        places = [{"type": "RECENT", "name": "A"},
                  {"type": "FAVORITE_HOME", "name": "Home"}]
        self.assertIs(api._home_place(places), places[1])

    def test_env_fallback(self):
        with mock.patch("config.HOME_LAT", 34.0), \
                mock.patch("config.HOME_LON", -118.0):
            home = api._home_place([])
        self.assertEqual(home["name"], "Home")
        self.assertEqual(home["lat"], 34.0)

    def test_no_home_anywhere(self):
        with mock.patch("config.HOME_LAT", float("nan")), \
                mock.patch("config.HOME_LON", float("nan")):
            self.assertIsNone(api._home_place([]))


class NavUnitTests(unittest.TestCase):
    def test_share_url_without_place_id_uses_maps_query(self):
        url = nav.share_url({"name": "A", "address": "", "lat": 34.0,
                             "lon": -118.0, "place_id": None})
        self.assertIn("google.com/maps", url)

    def test_share_url_with_place_id_uses_telenav(self):
        url = nav.share_url({"name": "A", "address": "1 Main St",
                             "lat": 34.0, "lon": -118.0,
                             "place_id": "ChIJtest"})
        self.assertIn("telenav.id", url)
        self.assertIn("entity_id=ChIJtest", url)

    def test_share_url_passthrough(self):
        self.assertEqual(nav.share_url({"url": "https://goo.gl/maps/x"}),
                         "https://goo.gl/maps/x")

    def test_trip_waypoints_target_soc_on_last_stop(self):
        wps = nav._trip_waypoints(
            {"lat": 34.0, "lon": -118.0},
            [{"name": "B", "address": "", "lat": 34.1, "lon": -118.1,
              "place_id": None}])
        self.assertEqual(len(wps), 2)
        self.assertEqual(wps[-1]["minArrivalSoC"], nav.TARGET_SOC)

    def test_haversine_known_distance(self):
        d = nav.haversine_mi(34.05, -118.25, 32.71, -117.16)
        self.assertTrue(105 < d < 120)

    def test_names_normalized(self):
        route = {"destinations": [{"name": " Alpha "}, {"name": "BETA"}]}
        self.assertEqual(nav._names(route), {"alpha", "beta"})
        self.assertEqual(nav._names(None), set())

    def test_start_send_job_refuses_a_second_send(self):
        with mock.patch.dict(nav._JOBS, {"x": {"state": "sending"}}):
            self.assertIsNone(nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "direct", False, None, {"lat": 34.0, "lon": -118.0}))

    def test_start_send_job_reaps_a_stale_send(self):
        import time as _time

        async def fake_send(*args):
            return {"destinations": []}

        stale = {"state": "sending", "ts": _time.time() - 3600}
        with mock.patch.dict(nav._JOBS, {"x": stale}), \
                mock.patch.object(nav, "_send_and_confirm_async",
                                  fake_send):
            job_id = nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "direct", False, None, {"lat": 34.0, "lon": -118.0})
        self.assertIsNotNone(job_id)
        self.assertEqual(stale["state"], "failed")

    def test_route_fingerprint(self):
        route = {"destinations": [{"name": "A", "lat": 1.0, "lon": 2.0,
                                   "place_id": "p"}],
                 "chargers": [{"name": "C", "minutes": 10}]}
        same = {"destinations": [{"name": "A", "lat": 1.0, "lon": 2.0,
                                  "place_id": "p"}],
                "chargers": [{"name": "C", "minutes": 10}]}
        self.assertEqual(nav._route_fingerprint(route),
                         nav._route_fingerprint(same))
        replanned = {"destinations": [{"name": "A", "lat": 1.00001,
                                       "lon": 2.00004,
                                       "place_id": "p"}],
                     "chargers": [{"name": "C", "minutes": 25}]}
        self.assertEqual(nav._route_fingerprint(route),
                         nav._route_fingerprint(replanned))
        changed = {"destinations": route["destinations"],
                   "chargers": [{"name": "D", "minutes": 12}]}
        self.assertNotEqual(nav._route_fingerprint(route),
                            nav._route_fingerprint(changed))
        self.assertIsNone(nav._route_fingerprint(None))


class NavMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.db"
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(db.SCHEMA)
        self.client = api.app.test_client()
        for target, value in [
            ("api.auth", DashboardAuth(SECRET, lambda: 1700000000)),
            ("api.DB_PATH", self.database),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_map_renders_at_the_wider_zoom(self):
        with mock.patch("report._render_point_map",
                        return_value=b"jpeg") as render:
            r = self.client.get("/api/nav/map?lat=33.6&lon=-117.7",
                                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(render.call_args.args, (33.6, -117.7))
        self.assertEqual(render.call_args.kwargs.get("z"), 15)

    def test_map_zoom_is_clamped_to_the_tile_ceiling(self):
        with mock.patch("report._render_point_map",
                        return_value=b"jpeg") as render:
            r = self.client.get(
                "/api/nav/map?lat=33.6&lon=-117.7&z=99", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(render.call_args.kwargs.get("z"), 16)

    def test_map_zoom_bad_value_uses_the_default(self):
        with mock.patch("report._render_point_map",
                        return_value=b"jpeg") as render:
            self.client.get("/api/nav/map?lat=33.6&lon=-117.7&z=abc",
                            headers=AUTH)
        self.assertEqual(render.call_args.kwargs.get("z"), 15)

    def test_lost_job_message_is_wired(self):
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("Lost track of that send", js)

    def test_places_failure_message_is_wired(self):
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("could not load just now", js)
        self.assertIn("places_fresh", js)

    def test_map_image_opens_google_maps(self):
        import report
        self.assertIn('id="navmaplink"', report.HTML_TEMPLATE)
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("google.com/maps/search/?api=1", js)
        self.assertIn("query_place_id", js)
        self.assertIn("&z=15", js)


class SavedPlacesCacheTests(unittest.TestCase):
    def setUp(self):
        self._orig = dict(api._saved_cache)
        api._saved_cache.update(ts=0.0, places=[])
        self.addCleanup(api._saved_cache.update, self._orig)

    def test_transient_failure_is_retried(self):
        place = {"name": "Home", "type": "FAVORITE_HOME"}
        with mock.patch("nav.saved_places",
                        side_effect=[Exception("timeout"),
                                     [place]]) as fetch:
            places, fresh = api._saved_places_cached()
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(fresh)
        self.assertEqual(places, [place])

    def test_persistent_failure_falls_back_not_fresh(self):
        with mock.patch("nav.saved_places",
                        side_effect=Exception("down")):
            places, fresh = api._saved_places_cached()
        self.assertFalse(fresh)
        self.assertEqual(places, [])


class ConfirmerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(nav, "parse_trip_info",
                                    lambda raw: raw)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def route(name, lat=34.0, lon=-118.0, chargers=()):
        return {"destinations": [{"name": name, "lat": lat,
                                  "lon": lon, "place_id": None}],
                "chargers": list(chargers)}

    def test_no_baseline_other_route_becomes_the_baseline(self):
        c = nav._Confirmer(None, "moon house")
        self.assertFalse(c.check(self.route("Irvine Spectrum")))
        # A second, unrelated transition is not the send landing.
        self.assertFalse(c.check(self.route("Newport Beach")))
        self.assertTrue(c.check(self.route("Moon House")))

    def test_baseline_unrelated_transition_does_not_confirm(self):
        c = nav._Confirmer(self.route("Home"), "moon house")
        self.assertFalse(c.check(self.route("Newport Beach")))
        self.assertTrue(c.check(self.route("Moon House")))

    def test_round_trip_needs_the_home_leg_without_baseline(self):
        both = {"destinations": [
            {"name": "Moon House", "lat": 34.0, "lon": -118.0,
             "place_id": None},
            {"name": "Home", "lat": 34.1, "lon": -118.1,
             "place_id": None}], "chargers": []}
        c = nav._Confirmer(None, "moon house", home="home")
        self.assertTrue(c.check(both))
        c = nav._Confirmer(None, "moon house", home="home")
        # A pre-existing one-way route is not the round trip.
        self.assertFalse(c.check(self.route("Moon House")))
        self.assertTrue(c.check(both))

    def test_no_baseline_sent_place_confirms(self):
        c = nav._Confirmer(None, "moon house")
        self.assertTrue(c.check(self.route("Moon House")))

    def test_baseline_replan_does_not_confirm(self):
        base = self.route("Moon House",
                          chargers=[{"name": "C", "minutes": 10}])
        c = nav._Confirmer(base, "moon house")
        jittered = self.route(
            "Moon House", lat=34.0 + 1e-5,
            chargers=[{"name": "C", "minutes": 22}])
        self.assertFalse(c.check(jittered))

    def test_baseline_new_destination_confirms(self):
        c = nav._Confirmer(self.route("Home"), "moon house")
        self.assertTrue(c.check(self.route("Moon House")))




class NavSendUpgradeTests(unittest.TestCase):
    """Address results (no place id, no url) now upgrade to trips."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.db"
        with closing(sqlite3.connect(self.database)) as con:
            con.executescript(db.SCHEMA)
        self.client = api.app.test_client()
        for target, value in [
            ("api.auth", DashboardAuth(SECRET, lambda: 1700000000)),
            ("api.DB_PATH", self.database),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _seed_car_row(self):
        with closing(sqlite3.connect(self.database)) as con, con:
            con.execute(
                "INSERT INTO snapshots (ts, soc, range_mi, lat, lon)"
                " VALUES (1700000000, 55.0, 180.0, 34.0, -118.0)")

    def test_address_result_without_place_id_upgrades_to_trip(self):
        self._seed_car_row()
        with mock.patch("api._saved_places_cached",
                        return_value=([], True)), \
                mock.patch("nav.start_send_job",
                           return_value="job1") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1,
                                "lon": -118.1},
                      "mode": "direct"},
                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(start.call_args.args[1], "trip")

    def test_pasted_link_stays_direct(self):
        with mock.patch("nav.start_send_job",
                        return_value="job2") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"url":
                                "https://www.google.com/maps/place/X"},
                      "mode": "direct"},
                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(start.call_args.args[1], "direct")


    def test_named_search_result_upgrades_to_trip(self):
        self._seed_car_row()
        with mock.patch("api._saved_places_cached",
                        return_value=([], True)), \
                mock.patch("nav.start_send_job",
                           return_value="job3") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "Test Cafe", "lat": 34.1,
                                "lon": -118.1},
                      "mode": "direct"},
                headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(start.call_args.args[1], "trip")

    def test_upgrade_without_car_position_is_503(self):
        with mock.patch("nav.start_send_job",
                        return_value="job4") as start:
            r = self.client.post(
                "/api/nav/send",
                json={"place": {"name": "B", "lat": 34.1,
                                "lon": -118.1},
                      "mode": "direct"},
                headers=AUTH)
        self.assertEqual(r.status_code, 503)
        start.assert_not_called()


class ConfirmerCoordsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(nav, "parse_trip_info",
                                    lambda raw: raw)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def route(name, lat=34.0, lon=-118.0, chargers=()):
        return {"destinations": [{"name": name, "lat": lat,
                                  "lon": lon, "place_id": None}],
                "chargers": list(chargers)}

    def test_no_baseline_coords_do_not_confirm(self):
        # Without a baseline, a coords match cannot distinguish "our
        # send landed" from "the car was already going nearby": the
        # stale nearby route adopts as the baseline instead of
        # confirming. Only a new route to the sent coordinates can
        # confirm after that.
        c = nav._Confirmer(None, "test street",
                           want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.route("Business Name",
                                            34.001, -118.0)))
        self.assertTrue(c.check(self.route("Other Name",
                                           34.0, -118.0)))

    def test_no_baseline_different_coords_becomes_baseline(self):
        c = nav._Confirmer(None, "test street",
                           want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.route("Elsewhere",
                                            35.0, -119.0)))

    def test_baseline_coords_confirm_when_plan_changed(self):
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", want_coords=(34.0, -118.0))
        self.assertTrue(c.check(self.route("Business Name",
                                           34.0, -118.0)))

    def test_baseline_identical_report_does_not_confirm_via_coords(self):
        base = self.route("Test Street", 34.0, -118.0)
        c = nav._Confirmer(base, "test street",
                           want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.route("Test Street",
                                            34.0, -118.0)))

    def test_nearby_but_wrong_destination_does_not_confirm(self):
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.route("Wrong Place",
                                            34.004, -118.0)))
        self.assertTrue(c.check(self.route("Business Name",
                                           34.001, -118.0)))

    def test_no_coords_means_name_matching_only(self):
        c = nav._Confirmer(None, "test street")
        self.assertFalse(c.check(self.route("Business Name",
                                            34.0, -118.0)))

    @staticmethod
    def multi_route(stops):
        return {"destinations": [{"name": n, "lat": la, "lon": lo,
                                  "place_id": None}
                                 for n, la, lo in stops],
                "chargers": []}

    def test_multistop_intermediate_near_final_elsewhere_does_not_confirm(self):
        # A foreign multi-stop route passing near the sent place on
        # its way elsewhere must not confirm: for a one-way send
        # only the final destination counts.
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.multi_route([
            ("Near Stop", 34.001, -118.0),
            ("Elsewhere", 35.0, -119.0)])))

    def test_multistop_ending_at_coords_confirms(self):
        # Positive control: a multi-stop route that ends at the
        # sent coordinates is going to the sent place.
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", want_coords=(34.0, -118.0))
        self.assertTrue(c.check(self.multi_route([
            ("Elsewhere", 35.0, -119.0),
            ("Business Name", 34.001, -118.0)])))

    def test_round_trip_first_stop_coords_confirms(self):
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", home="home",
                           want_coords=(34.0, -118.0))
        self.assertTrue(c.check(self.multi_route([
            ("Business Name", 34.001, -118.0),
            ("Home", 34.1, -118.1)])))

    def test_round_trip_single_leg_coords_confirms(self):
        # The car may report only the current leg while driving to
        # the place; the home leg need not be in the report yet for
        # the first stop to confirm.
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", home="home",
                           want_coords=(34.0, -118.0))
        self.assertTrue(c.check(self.multi_route([
            ("Business Name", 34.001, -118.0)])))

    def test_round_trip_intermediate_near_does_not_confirm(self):
        # A route passing near the place on its way elsewhere, even
        # with a home leg, is not the round trip that was sent: the
        # stop after the place must be home.
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", home="home",
                           want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.multi_route([
            ("Near Stop", 34.001, -118.0),
            ("Elsewhere", 35.0, -119.0),
            ("Home", 34.1, -118.1)])))

    def test_round_trip_trailing_destination_does_not_confirm(self):
        # A route that visits the place and then home but continues
        # elsewhere does not end at home, so it is not the round
        # trip that was sent. The reported name differs from the
        # sent name, so this depends on coordinates, not names.
        c = nav._Confirmer(self.route("Home", 34.1, -118.1),
                           "test street", home="home",
                           want_coords=(34.0, -118.0))
        self.assertFalse(c.check(self.multi_route([
            ("Other Name", 34.001, -118.0),
            ("Home", 34.1, -118.1),
            ("Elsewhere", 35.0, -119.0)])))


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SearchPlacesTests(unittest.TestCase):
    ROWS = [
        {"lat": "34.05", "lon": "-118.25",
         "display_name": "123, Test Street, Testville, California,"
                         " 90000, United States"},
        {"lat": "34.06", "lon": "-118.26", "name": "Test Cafe",
         "display_name": "Test Cafe, 123 Test Street, Testville,"
                         " California, 90000, United States"},
    ]

    def setUp(self):
        payload = json.dumps(self.ROWS).encode("utf-8")
        patcher = mock.patch.object(nav.urllib.request, "urlopen",
                                    return_value=_FakeResp(payload))
        patcher.start()
        self.addCleanup(patcher.stop)
        self._orig_last = nav._last_search
        nav._last_search = 0.0
        self.addCleanup(setattr, nav, "_last_search", self._orig_last)

    def test_address_row_uses_full_display_as_name(self):
        results = nav.search_places("123 test street")
        self.assertEqual(results[0]["name"], self.ROWS[0]["display_name"])
        self.assertIsNone(results[0]["place_id"])
        self.assertEqual((results[0]["lat"], results[0]["lon"]),
                         (34.05, -118.25))

    def test_named_row_keeps_its_name(self):
        results = nav.search_places("test cafe")
        self.assertEqual(results[1]["name"], "Test Cafe")
        self.assertIsNone(results[1]["place_id"])


class SendJobSentTests(unittest.TestCase):
    def _wait_until_not_sending(self, job_id, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if nav._JOBS[job_id]["state"] != "sending":
                return nav._JOBS[job_id]
            time.sleep(0.05)
        return nav._JOBS[job_id]

    def test_cloud_accepted_unconfirmed_send_reports_sent(self):
        async def fake(place, mode, round_trip, home, origin, job):
            job["cloud_accepted"] = True
            return None

        with mock.patch.dict(nav._JOBS, {}), \
                mock.patch.object(nav, "_send_and_confirm_async", fake):
            job_id = nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "trip", False, None, {"lat": 34.0, "lon": -118.0})
            job = self._wait_until_not_sending(job_id)
        self.assertEqual(job["state"], "sent")
        self.assertIsNone(job["error"])

    def test_unaccepted_send_reports_failed(self):
        async def fake(place, mode, round_trip, home, origin, job):
            return None

        with mock.patch.dict(nav._JOBS, {}), \
                mock.patch.object(nav, "_send_and_confirm_async", fake):
            job_id = nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "trip", False, None, {"lat": 34.0, "lon": -118.0})
            job = self._wait_until_not_sending(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"],
                         "The car never confirmed the route. "
                         "It may not have received the send.")


    def test_accepted_then_error_reports_sent(self):
        async def fake(place, mode, round_trip, home, origin, job):
            job["cloud_accepted"] = True
            raise RuntimeError("retry failed")

        with mock.patch.dict(nav._JOBS, {}), \
                mock.patch.object(nav, "_send_and_confirm_async", fake):
            job_id = nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "trip", False, None, {"lat": 34.0, "lon": -118.0})
            job = self._wait_until_not_sending(job_id)
        self.assertEqual(job["state"], "sent")
        self.assertIsNone(job["error"])

    def test_unaccepted_error_reports_failed(self):
        async def fake(place, mode, round_trip, home, origin, job):
            raise RuntimeError("cloud rejected")

        with mock.patch.dict(nav._JOBS, {}), \
                mock.patch.object(nav, "_send_and_confirm_async", fake):
            job_id = nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "trip", False, None, {"lat": 34.0, "lon": -118.0})
            job = self._wait_until_not_sending(job_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"], "cloud rejected")


class JsSentTests(unittest.TestCase):
    def test_sent_message_is_wired(self):
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("Sent. Verify in the car app.", js)

    def test_failed_branch_precedes_sent_branch(self):
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        failed = js.index("} else if (job.state === 'failed') {")
        sent = js.index(
            "} else if (job.state === 'sent' || job.cloud_accepted) {")
        self.assertLess(failed, sent)

    def test_fetch_failure_guard_tracks_acceptance(self):
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("accepted = true;", js)
        self.assertIn("!accepted", js)


if __name__ == "__main__":
    unittest.main()
