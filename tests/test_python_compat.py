"""
Architectural guards for "KiForge loads inside KiCad, on every platform".

KiCad bundles its own Python and the version differs per platform -- macOS 10.x
ships 3.9.13, other platforms ship newer 3.x -- so the supported interpreter is
a *range*, and shipped code has to run across all of it. Three things went wrong
at once when that was left implicit:

1. Nothing declared the floor, so 3.10+ syntax drifted in.
2. Nothing outside a real KiCad ever imported ``plugins/kiforge_studio.py``, so
   the resulting ``TypeError`` reached users.
3. The bootstrap swallowed the error, so a broken install looked exactly like a
   working one.

This module guards all three, in layers that fail at different times:

* :class:`TestImportGate` -- actually imports every shipped module under a fake
  KiCad runtime and asserts a toolbar button gets registered. The strongest
  check; catches anything the running interpreter would reject.
* :class:`TestStaticCompatibility` -- AST checks that hold even when the tests
  run on a *newer* interpreter than the floor, so a 3.12-only leg still catches
  3.10+ syntax.
* :class:`TestBaselineConsistency` -- the floor is declared in four places
  (bootstrap, pyproject, ruff, CI matrix); this fails if they drift apart.
* :class:`TestLoadFailureIsVisible` -- the "never fail silently" invariant.
"""
import ast
import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (REPO_ROOT, TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import kicad_runtime_stub

PYPROJECT = os.path.join(REPO_ROOT, "pyproject.toml")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "test-action.yml")

# Files whose module-level code KiCad or the packager executes. Not
# ``plugins/kiforge.py`` -- that is a build artifact the packager copies in.
SHIPPED_SOURCES = (
    "kiforge.py",
    "package_plugin.py",
    os.path.join("plugins", "__init__.py"),
    os.path.join("plugins", "kiforge_studio.py"),
)


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _min_python():
    """The declared floor, read from the bootstrap that enforces it at runtime."""
    import plugins
    return plugins.MIN_PYTHON


def _format(version):
    return "%d.%d" % (version[0], version[1])


class TestImportGate(unittest.TestCase):
    """
    Every shipped module must execute to completion and register a toolbar button.

    Run in a subprocess so the fake KiCad runtime cannot leak into the rest of
    the suite, and so the modules are imported cold rather than from whatever
    another test already cached.
    """

    def test_shipped_modules_import_and_register_under_kicad(self):
        result = subprocess.run(
            [sys.executable, os.path.join("tests", "kicad_runtime_stub.py")],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            "KiForge does not load on Python %s.\n%s\n%s"
            % (sys.version.split()[0], result.stdout, result.stderr),
        )


class TestStaticCompatibility(unittest.TestCase):
    """
    AST-level checks, so a CI leg on a newer interpreter still catches 3.10+ code.

    The import gate above only proves the code runs on *this* interpreter. These
    two rules encode what makes it run on the floor as well.
    """

    def test_shipped_sources_parse_on_the_floor(self):
        """Reject syntax newer than the oldest supported interpreter."""
        floor = _min_python()
        for relative_path in SHIPPED_SOURCES:
            with self.subTest(module=relative_path):
                try:
                    ast.parse(_read(relative_path), filename=relative_path, feature_version=floor)
                except SyntaxError as exc:
                    self.fail(
                        "%s uses syntax newer than Python %s, which KiCad bundles: %s"
                        % (relative_path, _format(floor), exc)
                    )

    def test_pep604_annotations_are_postponed(self):
        """
        ``X | None`` parses on 3.9 but raises TypeError when evaluated at def time.

        ``from __future__ import annotations`` keeps annotations as strings. This
        mirrors ruff's FA102, so the rule holds even where ruff is not run.
        """
        for relative_path in SHIPPED_SOURCES:
            with self.subTest(module=relative_path):
                tree = ast.parse(_read(relative_path), filename=relative_path)
                if not any(_has_pep604_union(a) for a in _annotations(tree)):
                    continue
                postponed = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "__future__"
                    and any(alias.name == "annotations" for alias in node.names)
                    for node in tree.body
                )
                self.assertTrue(
                    postponed,
                    "%s uses PEP 604 (X | None) annotations, which raise TypeError at "
                    "import time on Python %s. Add 'from __future__ import annotations' "
                    "below the module docstring." % (relative_path, _format(_min_python())),
                )


class TestBaselineConsistency(unittest.TestCase):
    """
    One floor, declared in four places that must agree.

    Runtime check, packaging/tooling metadata, lint target, and what CI actually
    exercises. Any one of them drifting silently re-opens the gap.
    """

    def test_pyproject_declares_the_same_floor(self):
        declared = re.search(r'^min-python\s*=\s*"(\d+)\.(\d+)"', _read("pyproject.toml"), re.M)
        self.assertIsNotNone(declared, "pyproject.toml is missing [tool.kiforge] min-python")
        self.assertEqual(
            (int(declared.group(1)), int(declared.group(2))),
            _min_python(),
            "pyproject.toml min-python disagrees with plugins.MIN_PYTHON",
        )

    def test_ruff_targets_the_same_floor(self):
        target = re.search(r'^target-version\s*=\s*"py(\d)(\d+)"', _read("pyproject.toml"), re.M)
        self.assertIsNotNone(target, "pyproject.toml is missing [tool.ruff] target-version")
        self.assertEqual(
            (int(target.group(1)), int(target.group(2))),
            _min_python(),
            "ruff target-version disagrees with plugins.MIN_PYTHON, so FA102 would "
            "stop flagging PEP 604 annotations that break on the floor",
        )

    def test_ci_exercises_the_floor_and_nothing_below_it(self):
        floor = _min_python()
        versions = set()
        for line in _read(".github", "workflows", "test-action.yml").splitlines():
            if "python-version:" in line and "[" in line:
                versions.update(
                    (int(major), int(minor))
                    for major, minor in re.findall(r"'(\d+)\.(\d+)'", line)
                )
        self.assertTrue(versions, "no python-version matrix found in the CI workflow")
        self.assertIn(
            floor,
            versions,
            "CI does not run on Python %s, the declared floor" % _format(floor),
        )
        below = sorted(v for v in versions if v < floor)
        self.assertEqual(below, [], "CI runs interpreters below the declared floor: %s" % below)


class TestLoadFailureIsVisible(unittest.TestCase):
    """
    The invariant: if KiForge is installed, something always appears in the toolbar.

    Either the plugin, or a button explaining why it could not load. A silent
    no-op is what made the original bug so hard to diagnose -- PCM reported the
    package installed and nothing appeared.
    """

    def setUp(self):
        self._saved = {name: sys.modules.get(name) for name in ("wx", "pcbnew")}
        self.pcbnew = kicad_runtime_stub.install()
        import plugins
        self.plugins = plugins
        self._saved_min = plugins.MIN_PYTHON
        self._saved_register = plugins._register_failure_plugin
        self._saved_studio = sys.modules.get("plugins.kiforge_studio")

    def tearDown(self):
        self.plugins.MIN_PYTHON = self._saved_min
        self.plugins._register_failure_plugin = self._saved_register
        if self._saved_studio is None:
            sys.modules.pop("plugins.kiforge_studio", None)
        else:
            sys.modules["plugins.kiforge_studio"] = self._saved_studio
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        del kicad_runtime_stub.REGISTERED[:]

    def _break_studio_import(self):
        """``None`` in sys.modules makes the submodule import raise ImportError."""
        sys.modules["plugins.kiforge_studio"] = None

    def test_broken_plugin_still_registers_a_diagnostic_button(self):
        self._break_studio_import()

        # assertLogs doubles as a check that the failure is logged, and keeps the
        # deliberate error out of the test run's stderr.
        with self.assertLogs("KiForge", level="ERROR"):
            self.assertFalse(self.plugins.load())

        registered = kicad_runtime_stub.REGISTERED
        self.assertEqual(len(registered), 1, "no stand-in plugin was registered")
        self.assertTrue(registered[0].show_toolbar_button, "the stand-in has no toolbar button")
        self.assertIn("KiForge", registered[0].name)
        self.assertIn("could not start", registered[0].description)

    def test_unsupported_interpreter_is_reported_before_anything_is_imported(self):
        self.plugins.MIN_PYTHON = (99, 0)

        self.assertFalse(self.plugins.load())

        registered = kicad_runtime_stub.REGISTERED
        self.assertEqual(len(registered), 1)
        self.assertIn("older than", registered[0].description)

    def test_failure_is_raised_when_no_diagnostic_can_be_registered(self):
        """Last resort: if even the stand-in fails, KiCad must see the traceback."""
        self._break_studio_import()
        self.plugins._register_failure_plugin = lambda summary, detail: False

        with self.assertLogs("KiForge", level="ERROR"):
            with self.assertRaises(ImportError):
                self.plugins.load()


def _annotations(tree):
    """Yield every expression appearing in an annotation position."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation
        elif isinstance(node, ast.arg) and node.annotation is not None:
            yield node.annotation


def _has_pep604_union(expr):
    return any(
        isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr)
        for child in ast.walk(expr)
    )


if __name__ == "__main__":
    unittest.main()
