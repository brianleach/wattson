import sqlite3
import unittest

import tires


def decoded(fl=2.90, fr=2.90, rl=2.90, rr=2.90,
            fl_status="OK", fr_status="OK", rl_status="OK", rr_status="OK"):
    """Build a decoded Parallax tire dict (pressures in bar)."""
    return {
        "tirePressureFrontLeft": fl, "tirePressureStatusFrontLeft": fl_status,
        "tirePressureFrontRight": fr, "tirePressureStatusFrontRight": fr_status,
        "tirePressureRearLeft": rl, "tirePressureStatusRearLeft": rl_status,
        "tirePressureRearRight": rr, "tirePressureStatusRearRight": rr_status,
    }


def level_at(psi, siblings=(42.0, 42.0, 42.0), status="OK"):
    return tires._low_level(status, psi, list(siblings))


class LowLevelTests(unittest.TestCase):
    def test_uniform_boundaries(self):
        # Siblings at the same pressure: only the %-below-target rules
        # apply (target defaults to 42: warn < 37.8, crit < 33.6).
        for psi, expected in [(42.0, 0), (37.9, 0), (37.7, 1),
                              (33.7, 1), (33.5, 2)]:
            with self.subTest(psi=psi):
                self.assertEqual(
                    level_at(psi, siblings=(psi, psi, psi)), expected)

    def test_sibling_delta_is_warn(self):
        # 39 PSI is fine against the target, but 5 PSI below siblings
        # at 44 means a likely slow leak: warn.
        self.assertEqual(level_at(39.0, siblings=(44.0, 44.0, 44.0)), 1)

    def test_tpms_status_flag_is_crit(self):
        self.assertEqual(level_at(40.0, status="LOW"), 2)
        self.assertEqual(level_at(42.0, status="Warning"), 2)

    def test_none_pressure_with_ok_status_is_fine(self):
        self.assertEqual(level_at(None), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)

    def test_reading_stored_in_bar_and_levels_upserted(self):
        tires.store_and_evaluate(self.con, decoded(), now=1700000000)
        row = self.con.execute(
            "SELECT ts, fl, fr, rl, rr, fl_status FROM tire_readings"
        ).fetchone()
        self.assertEqual(row, (1700000000, 2.90, 2.90, 2.90, 2.90, "OK"))
        state = dict(self.con.execute(
            "SELECT tire, low FROM tire_alert_state"))
        self.assertEqual(state, {"fl": 0, "fr": 0, "rl": 0, "rr": 0})

    def test_low_tire_level_is_stored(self):
        # FL at 2.30 bar = 33.4 PSI: below the 33.6 crit line.
        tires.store_and_evaluate(self.con, decoded(fl=2.30), now=1700000000)
        state = dict(self.con.execute(
            "SELECT tire, low FROM tire_alert_state"))
        self.assertEqual(state["fl"], 2)
        self.assertEqual(state["fr"], 0)

    def test_no_alert_events_table_is_created(self):
        tires.store_and_evaluate(self.con, decoded(fl=2.30), now=1700000000)
        tables = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("tire_readings", tables)
        self.assertIn("tire_alert_state", tables)
        self.assertNotIn("tire_alert_events", tables)


if __name__ == "__main__":
    unittest.main()
