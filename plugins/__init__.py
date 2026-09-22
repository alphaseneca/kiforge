"""
KiForge plugin package for KiCad 10 — load bootstrap.

This module is the only code KiCad executes unconditionally, so it is written
to the lowest common denominator: **stdlib only, no annotations, no syntax
newer than Python 3.6**. It must be able to run on an interpreter it does not
support in order to report that it does not support it.

Why it is shaped this way
-------------------------
KiCad bundles its own Python and the version differs per platform (macOS 10.x
ships 3.9). KiCad's plugin loader wraps each package import in a bare
``except:`` and only records the traceback in ``pcbnew.GetWizardsBackTrace()``,
which nothing surfaces prominently. A plugin that raises during import is
therefore indistinguishable from a plugin that is not installed: PCM reports it
as installed, and no toolbar button ever appears.

So loading is split in two:

* :data:`MIN_PYTHON` — the declared interpreter floor, checked *before* any
  KiForge module is touched. Mirrored by ``[tool.kiforge] min-python`` and
  ``[tool.ruff] target-version`` in ``pyproject.toml`` and by the CI matrix;
  ``tests/test_python_compat.py`` fails if they drift.
* :func:`load` — imports and registers the real plugin, and on *any* failure
  registers :func:`_register_failure_plugin` instead, a stand-in toolbar button
  that reports what went wrong. If even that cannot be registered, the original
  exception is re-raised so KiCad's own backtrace is left holding it.

The invariant: **if KiForge is installed, something always appears in the
toolbar** — either the plugin or an explanation.
"""
import sys

# Stamped by package_plugin.py at package time -- release zips carry the tag,
# local zips carry LOCAL_DEV_VERSION. The tracked value is the unreleased
# default, so a report from a git checkout says 0.0.0 rather than claiming a
# release number it is not. The packager writes a staged copy and never edits
# this file, so packaging leaves the working tree clean.
__version__ = "0.0.0"
__author__ = "alphaseneca"

# Oldest interpreter KiForge supports, as a range floor rather than an exact
# build: KiCad 10 bundles 3.9.13 on macOS today and a newer 3.x elsewhere, and
# both must work. Mirrored by pyproject.toml ([tool.kiforge] min-python and
# ruff target-version) and the CI matrix; a test asserts all four agree.
MIN_PYTHON = (3, 9)

_LOG_BASENAME = "kiforge-load-error.log"


def _format_version(version_info):
    """Render a version tuple as ``X.Y`` for messages."""
    return "%d.%d" % (version_info[0], version_info[1])


def _environment_report():
    """Collect the facts that make a load failure diagnosable, best effort."""
    lines = [
        "KiForge %s" % __version__,
        "Python %s (%s)" % (sys.version.split()[0], sys.executable or "embedded"),
        "Minimum supported Python: %s" % _format_version(MIN_PYTHON),
        "Platform: %s" % sys.platform,
    ]
    try:
        import pcbnew
        lines.append("KiCad: %s" % pcbnew.GetBuildVersion())
    except Exception:
        lines.append("KiCad: unavailable")
    try:
        import os
        lines.append("Install path: %s" % os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    return "\n".join(lines)


def _write_log(detail):
    """Write the failure report somewhere the user can copy from. Returns path or None."""
    try:
        import os
        import tempfile
        path = os.path.join(tempfile.gettempdir(), _LOG_BASENAME)
        # Explicit UTF-8: the default is cp1252 on Windows, and a traceback can
        # quote source lines containing non-ASCII. A diagnostic writer must not
        # be able to fail on the text it exists to record.
        handle = open(path, "w", encoding="utf-8", errors="replace")
        try:
            handle.write(detail)
        finally:
            handle.close()
        return path
    except Exception:
        return None


def _show_failure_dialog(summary, detail):
    """Show the failure report in a dialog if wx is usable, else print it."""
    try:
        import wx
        wx.MessageBox(detail, "KiForge failed to load: %s" % summary, wx.OK | wx.ICON_ERROR)
        return
    except Exception:
        pass
    try:
        sys.stderr.write("KiForge failed to load: %s\n%s\n" % (summary, detail))
    except Exception:
        # A console that cannot encode the report is not a reason to lose it;
        # the toolbar button and the log file still carry the same text.
        pass


def _register_failure_plugin(summary, detail):
    """
    Register a stand-in toolbar button that reports why KiForge did not load.

    Returns True if the button was registered. The button is what makes a broken
    install visible: clicking it shows the same report written to the log file.
    """
    try:
        import os
        import pcbnew

        report = "%s\n\n%s" % (_environment_report(), detail)
        log_path = _write_log(report)
        if log_path:
            report = "%s\nReport written to: %s" % (report, log_path)

        class _KiForgeLoadFailure(pcbnew.ActionPlugin):
            """Placeholder ActionPlugin shown when the real plugin cannot load."""

            def defaults(self):
                self.name = "KiForge (failed to load)"
                self.category = "Manufacturing"
                self.description = "KiForge could not start: %s" % summary
                self.show_toolbar_button = True
                icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
                self.icon_file_name = icon
                self.dark_icon_file_name = icon

            def Run(self):
                _show_failure_dialog(summary, report)

        _KiForgeLoadFailure().register()
        return True
    except Exception:
        return False


def load():
    """
    Import and register KiForge Studio inside KiCad.

    Returns True when the real plugin registered. On failure a diagnostic button
    is registered in its place and False is returned; the original exception is
    re-raised only when no diagnostic could be registered, so that KiCad's own
    plugin backtrace is never the *only* record of the failure.
    """
    if sys.version_info < MIN_PYTHON:
        summary = "Python %s is older than the required %s" % (
            _format_version(sys.version_info),
            _format_version(MIN_PYTHON),
        )
        if not _register_failure_plugin(summary, _environment_report()):
            raise RuntimeError("KiForge requires Python %s or newer" % _format_version(MIN_PYTHON))
        return False

    try:
        import importlib
        pkg = __name__
        for mod in list(sys.modules.keys()):
            if mod.startswith(pkg + ".") and mod != pkg:
                try:
                    if sys.modules.get(mod) is not None:
                        importlib.reload(sys.modules[mod])
                except Exception:
                    pass

        from .kiforge_studio import ExporterPlugin
        ExporterPlugin().register()
        return True
    except Exception as exc:
        import logging
        import traceback

        detail = traceback.format_exc()
        logging.getLogger("KiForge").error("Failed to register KiForge plugin: %s", exc)
        if not _register_failure_plugin("%s: %s" % (type(exc).__name__, exc), detail):
            # Nothing visible succeeded -- let KiCad record the traceback rather
            # than swallowing the only remaining evidence.
            raise
        return False


# ``pcbnew`` in sys.modules is the reliable signal that Python is running inside
# KiCad -- not during CLI, unit tests, or packaging, where registering would
# trigger C++ assertions.
if 'pcbnew' in sys.modules:
    load()
