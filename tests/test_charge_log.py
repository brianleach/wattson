import re
import unittest
from unittest import mock

import report

T = 1790000000


def session(i):
    return {"start_ts": T - i * 86400, "end_ts": T - i * 86400 + 1800,
            "kwh_added": 10.0}


def rendered(n):
    with mock.patch("report._charge_row_html",
                    side_effect=lambda c, *a, **k:
                    f'<details class="chargerow">{c["start_ts"]}</details>'), \
            mock.patch("report._charge_month_summary", return_value=""):
        return report._charges_html([session(i) for i in range(n)], [])


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


if __name__ == "__main__":
    unittest.main()
