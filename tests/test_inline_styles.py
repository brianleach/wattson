"""Guard: inline styles must not creep back into the dashboard.

All presentation lives in static/dashboard.css as classes, and
dashboard.js toggles classes instead of setting style properties.
The only style= attributes allowed are CSS custom properties
(--p, --l, --w) carrying per-render computed values (bar widths,
activity positions) into those classes.

The property name itself must be literal in the source. A style
whose name or whole value is interpolated at runtime cannot be
checked, so the guard fails it and the author has to restructure
(the custom-property pattern always can).
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE_ATTR = re.compile(r"""style\s*=\s*"([^"]*)"|style\s*=\s*'([^']*)'""",
                        re.IGNORECASE)


def _check_value(value):
    for decl in value.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        # \x01 stands in for an interpolated expression in a
        # reconstructed f-string. Interpolated VALUES are fine
        # (--p:{...}%); an interpolated property NAME leaves
        # nothing literal to check, so it fails.
        prop = decl.split(":", 1)[0].strip().replace("\x01", "")
        assert prop.startswith("--"), (
            f"inline style declaration is not a custom property: {decl!r}")


def _scan(text):
    for match in STYLE_ATTR.finditer(text):
        _check_value(match.group(1) if match.group(1) is not None
                     else match.group(2))


def _joined(node):
    parts = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue) \
                and isinstance(part.value, ast.Constant) \
                and isinstance(part.value.value, str):
            # A constant interpolated into the template is known
            # text (f'<div {"style"}="...">'), not a placeholder.
            parts.append(part.value.value)
        else:
            parts.append("\x01")
    return "".join(parts)


def _module_strings(tree):
    """Module-level name = 'literal' assignments, so a string
    built as prefix + '...' with prefix defined in the module is
    still reconstructed whole."""
    out = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) \
                and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            out[node.targets[0].id] = node.value.value
    return out


def _iter_strings(tree):
    """Every string the source can produce: plain constants,
    f-strings (expressions as \\x01), and +-joined combinations,
    so a style attribute split across literals is still seen
    whole."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) \
                and isinstance(node.value, str):
            yield node.value
        elif isinstance(node, ast.JoinedStr):
            yield _joined(node)
        elif isinstance(node, ast.BinOp) \
                and isinstance(node.op, ast.Add):
            parts, stack, ok = [], [node], True
            while stack:
                cur = stack.pop()
                if isinstance(cur, ast.BinOp) \
                        and isinstance(cur.op, ast.Add):
                    stack.extend([cur.right, cur.left])
                elif isinstance(cur, ast.Constant) \
                        and isinstance(cur.value, str):
                    parts.append(cur.value)
                elif isinstance(cur, ast.JoinedStr):
                    parts.append(_joined(cur))
                elif isinstance(cur, ast.Name) \
                        and cur.id in _module_strings(tree):
                    parts.append(_module_strings(tree)[cur.id])
                else:
                    ok = False
                    break
            if ok:
                yield "".join(parts)


class InlineStyleGuardTests(unittest.TestCase):
    def test_report_source(self):
        tree = ast.parse((ROOT / "report.py").read_text(encoding="utf-8"))
        for text in _iter_strings(tree):
            _scan(text)
            self.assertNotIn(".style.", text)

    def test_template(self):
        import report
        _scan(report.HTML_TEMPLATE)

    def test_dashboard_js(self):
        js = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
        self.assertNotIn('style="', js)
        self.assertNotIn("style='", js)
        self.assertNotIn(".style.", js)


class GuardEvasionTests(unittest.TestCase):
    """Inputs an earlier version of this guard let through."""

    def assertCaught(self, source):
        with self.assertRaises(AssertionError):
            for text in _iter_strings(ast.parse(source)):
                _scan(text)

    def test_interpolated_value_caught(self):
        self.assertCaught("x = f'<div style=\"{css}\"></div>'")

    def test_interpolated_property_caught(self):
        self.assertCaught("x = f'<div style=\"{k}:{v}\"></div>'")

    def test_split_literals_caught(self):
        self.assertCaught("x = '<div style=\"' + 'color:red\">'")

    def test_single_quotes_caught(self):
        self.assertCaught('x = "<div style=\'color:red\'>"')

    def test_attribute_case_caught(self):
        self.assertCaught('x = \'<div Style="color:red"></div>\'')

    def test_interpolated_attribute_name_caught(self):
        self.assertCaught('x = f\'<div {"style"}="color:red"></div>\'')

    def test_variable_split_caught(self):
        self.assertCaught(
            'prefix = \'<div style="\'\nx = prefix + \'color:red">\'')

    def test_custom_property_still_allowed(self):
        for text in _iter_strings(
                ast.parse("x = f'<div style=\"--p:{v}%\"></div>'")):
            _scan(text)  # must not raise


if __name__ == "__main__":
    unittest.main()
