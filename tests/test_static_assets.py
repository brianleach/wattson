import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import db
import report


class StaticAssetsTests(unittest.TestCase):
    """The dashboard's CSS/JS live in static/dashboard.css and
    static/dashboard.js. The served dashboard links them; demo
    builds inline them so the demo stays a single file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output = root / "dashboard.html"
        for target, value in [
            ("db.DB_PATH", root / "tracker.db"),
            ("report.REPORT_PATH", self.output),
            ("report.TOKENS_PATH", root / "missing-tokens"),
            ("report._fmt_time", lambda ts: "10:00 am"),
        ]:
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init()
        with db.connect() as con:
            con.executemany(
                "INSERT INTO snapshots (ts, soc) VALUES (?, ?)",
                [(1700000000, 60)])

    def test_served_dashboard_links_static_assets(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("WATTSON_DEMO", None)
            report.generate()
        html = self.output.read_text()
        self.assertIn('<link rel="stylesheet" href="/dashboard.css">', html)
        self.assertIn('<script src="/dashboard.js"></script>', html)
        self.assertNotIn("var _navCar", html)
        self.assertNotIn("--bg:#0f0f11", html)

    def test_demo_dashboard_inlines_static_assets(self):
        with mock.patch.dict(os.environ, {"WATTSON_DEMO": "1"}):
            report.generate()
        html = self.output.read_text()
        self.assertIn("var _navCar", html)
        self.assertIn("--bg:#0f0f11", html)
        self.assertNotIn('<script src="/dashboard.js">', html)
        self.assertNotIn(
            '<link rel="stylesheet" href="/dashboard.css">', html)

    def test_template_has_no_inline_style_or_script_blocks(self):
        self.assertNotIn("<style>", report.HTML_TEMPLATE)
        self.assertNotIn("__SCRIPT__", report.HTML_TEMPLATE)
        self.assertFalse(hasattr(report, "JS_TEMPLATE"))
        self.assertIn("__STYLES__", report.HTML_TEMPLATE)
        self.assertIn("__SCRIPTS__", report.HTML_TEMPLATE)

    def test_asset_files_exist_and_cover_the_old_inline_code(self):
        static = Path(report.__file__).resolve().parent / "static"
        css = (static / "dashboard.css").read_text()
        js = (static / "dashboard.js").read_text()
        self.assertIn("--bg:#0f0f11", css)
        for marker in ("var _navCar", "scrollRaf", "_costChargeId",
                       "poll-age"):
            self.assertIn(marker, js)


if __name__ == "__main__":
    unittest.main()
