"""Guard: inline styles must not creep back into the dashboard.

All presentation lives in static/dashboard.css as classes, and
dashboard.js toggles classes instead of setting style properties.
The only style= attributes allowed are CSS custom properties
(--p, --l, --w) carrying per-render computed values (bar widths,
activity positions) into those classes.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE_ATTR = re.compile(r'style="([^"]*)"', re.S)


def _check_value(value):
    for decl in value.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        prop = decl.split(":", 1)[0].strip()
        # \x01 stands in for an interpolated expression in a
        # reconstructed f-string; a declaration whose property name
        # is computed cannot be checked and is allowed.
        assert prop.startswith("--") or "\x01" in prop, (
            f"inline style declaration is not a custom property: {decl!r}")


def _scan(text):
    for value in STYLE_ATTR.findall(text):
        _check_value(value)


class InlineStyleGuardTests(unittest.TestCase):
    def test_report_source(self):
        tree = ast.parse((ROOT / "report.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) \
                    and isinstance(node.value, str):
                _scan(node.value)
                self.assertNotIn(".style.", node.value)
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    part.value if isinstance(part, ast.Constant) else "\x01"
                    for part in node.values)
                _scan(text)

    def test_template(self):
        import report
        _scan(report.HTML_TEMPLATE)

    def test_dashboard_js(self):
        js = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn('style="', js)
        self.assertNotIn("style='", js)
        self.assertNotIn(".style.", js)


if __name__ == "__main__":
    unittest.main()
