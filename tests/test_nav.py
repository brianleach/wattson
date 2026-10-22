"""Send destination nav: gate, POST guards, place validation, helpers.

The nav routes act on the car and read its saved places and live
location, so unlike the rest of the API they require a dashboard
token to be set at all (see api._nav_gate).
"""
import sqlite3
import tempfile
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

    def test_send_in_progress(self):
        self.assertFalse(nav.send_in_progress())
        with mock.patch.dict(nav._JOBS, {"x": {"state": "sending"}}):
            self.assertTrue(nav.send_in_progress())

    def test_start_send_job_refuses_a_second_send(self):
        with mock.patch.dict(nav._JOBS, {"x": {"state": "sending"}}):
            self.assertIsNone(nav.start_send_job(
                {"name": "B", "lat": 34.1, "lon": -118.1},
                "direct", False, None, {"lat": 34.0, "lon": -118.0}))

    def test_route_fingerprint(self):
        route = {"destinations": [{"name": "A", "lat": 1.0, "lon": 2.0,
                                   "place_id": "p"}],
                 "chargers": [{"name": "C", "minutes": 10}]}
        same = {"destinations": [{"name": "A", "lat": 1.0, "lon": 2.0,
                                  "place_id": "p"}],
                "chargers": [{"name": "C", "minutes": 10}]}
        self.assertEqual(nav._route_fingerprint(route),
                         nav._route_fingerprint(same))
        changed = {"destinations": route["destinations"],
                   "chargers": [{"name": "C", "minutes": 12}]}
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

    def test_map_image_opens_google_maps(self):
        import report
        self.assertIn('id="navmaplink"', report.HTML_TEMPLATE)
        js = (Path(__file__).parent.parent / "static" /
              "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("google.com/maps/search/?api=1", js)
        self.assertIn("query_place_id", js)


if __name__ == "__main__":
    unittest.main()
