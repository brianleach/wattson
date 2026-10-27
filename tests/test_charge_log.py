import re
import unittest
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


if __name__ == "__main__":
    unittest.main()
