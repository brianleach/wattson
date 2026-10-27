import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import report

T = 1790000000


def session(i, charge_type="AC"):
    return {"start_ts": T - i * 86400, "end_ts": T - i * 86400 + 1800,
            "kwh_added": 10.0, "charge_type": charge_type}


def rendered(n, types=None):
    with mock.patch("report._charge_row_html",
                    side_effect=lambda c, *a, **k:
                    f'<details class="chargerow">{c["start_ts"]}</details>'), \
            mock.patch("report._charge_month_summary", return_value=""):
        return report._charges_html(
            [session(i, (types or ["AC"] * n)[i]) for i in range(n)], [])


class ChargeLogPagingTests(unittest.TestCase):
    def test_rows_past_first_page_ship_hidden(self):
        html = rendered(report.CHARGE_LOG_PAGE + 2)
        rows = re.findall(r"<details[^>]*>", html)
        self.assertEqual(len(rows), report.CHARGE_LOG_PAGE + 2)
        self.assertEqual(sum("hidden" in r for r in rows), 2)
        self.assertNotIn("hidden", "".join(rows[:report.CHARGE_LOG_PAGE]))
        self.assertIn('id="chgshowmore"', html)

    def test_no_button_when_everything_fits(self):
        html = rendered(report.CHARGE_LOG_PAGE)
        self.assertNotIn("hidden", html)
        self.assertNotIn('id="chgshowmore"', html)



class ChargeLogFilterTests(unittest.TestCase):
    def test_rows_carry_their_charge_type(self):
        html = rendered(3, ["DC fast", "AC", "DC fast"])
        self.assertEqual(re.findall(r'data-type="(\w+)"', html),
                         ["dc", "ac", "dc"])

    def test_filter_shown_when_log_mixes_ac_and_dc(self):
        self.assertIn('id="chgfilter"', rendered(2, ["AC", "DC fast"]))

    def test_no_filter_when_every_session_is_one_kind(self):
        self.assertNotIn('id="chgfilter"', rendered(3))



class ChargeLogMonthTests(unittest.TestCase):
    def test_rows_carry_their_local_month(self):
        html = rendered(2)
        self.assertEqual(
            re.findall(r'data-month="([\d-]+)"', html),
            [report._ldt(T).strftime("%Y-%m"),
             report._ldt(T - 86400).strftime("%Y-%m")])

    def test_month_select_lists_latest_then_months_newest_first(self):
        starts = [T, T - 40 * 86400, T - 2 * 86400]
        html = report._charge_month_select(starts)
        months = sorted({report._ldt(t).strftime("%Y-%m") for t in starts},
                        reverse=True)
        self.assertEqual(re.findall(r'<option value="([^"]+)"', html),
                         ["latest"] + months)

    def test_no_month_select_without_sessions(self):
        self.assertEqual(report._charge_month_select([]), "")



class ChargeLogOpenRowTests(unittest.TestCase):
    def test_newest_row_opens_even_when_it_came_from_rivian(self):
        newer = {"start_ts": T + 3600, "kwh": 5.0, "miles": 10.0,
                 "is_dc": False}
        with mock.patch("report._charge_row_html",
                        return_value='<details class="chargerow">t</details>'), \
                mock.patch("report._backfill_row_html",
                           return_value='<details class="chargerow">b</details>'), \
                mock.patch("report._charge_month_summary", return_value=""):
            html = report._charges_html([session(0)], [newer])
        rows = re.findall(r"<details[^>]*>[tb]", html)
        self.assertEqual([("open" in r, r[-1]) for r in rows],
                         [(True, "b"), (False, "t")])


class ChargeMapBudgetTests(unittest.TestCase):
    def test_new_maps_stop_at_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(report, "REPORT_PATH",
                                  Path(tmp) / "dash.html"), \
                mock.patch.object(report, "_render_point_map",
                                  return_value=b"jpeg") as render, \
                mock.patch.object(report, "_map_renders_left", 2):
            maps = [report._point_map_html(30.0, -97.0, "x", f"charge-{i}")
                    for i in range(3)]
            # Already on disk: served without spending budget.
            again = report._point_map_html(30.0, -97.0, "x", "charge-0")
        self.assertEqual([bool(m) for m in maps], [True, True, False])
        self.assertTrue(again)
        self.assertEqual(render.call_count, 2)


if __name__ == "__main__":
    unittest.main()
