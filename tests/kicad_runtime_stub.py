"""
A stand-in KiCad runtime, so the plugin's load path can be exercised anywhere.

KiForge only ever runs inside KiCad, which supplies ``pcbnew`` and its own
``wx``. Neither can be installed from PyPI in a way that resembles what KiCad
bundles, so CI historically could not import ``plugins/kiforge_studio.py`` at
all -- ``tests/test_studio.py`` skips unless ``KIFORGE_RUN_GUI_TESTS=1``. That
blind spot is how a module-level ``TypeError`` shipped: nothing outside a real
KiCad ever imported the file.

This module closes it. It installs permissive fakes for ``wx`` and ``pcbnew``
into ``sys.modules``, with one deliberate exception: ``pcbnew.ActionPlugin`` is
a *real* class mirroring KiCad's contract (``__init__`` calls ``defaults()``;
``register()`` publishes the plugin), so registration can be asserted rather
than mocked away.

What this does and does not prove
---------------------------------
It proves every shipped module **executes to completion on this interpreter**
and that KiForge registers a toolbar button -- the exact property that broke.
It does not check wx layout, KiCad API semantics, or anything requiring a real
board; ``tests/test_studio.py`` covers behaviour against real wx.

Runnable standalone, which is how CI uses it across OS/Python combinations::

    python tests/kicad_runtime_stub.py
"""
import os
import sys
import types


class _StubMeta(type):
    """Metaclass making stub classes tolerate any attribute or flag arithmetic."""

    def __getattr__(cls, name):
        return make_stub(name)

    def __call__(cls, *args, **kwargs):
        return super().__call__()

    # wx exposes int flag constants that modules combine at import time, e.g.
    # ``style=wx.OK | wx.ICON_INFORMATION`` as a default argument value.
    def __or__(cls, other):
        return cls

    def __ror__(cls, other):
        return cls

    def __int__(cls):
        return 0

    def __index__(cls):
        return 0


class _Stub(object, metaclass=_StubMeta):
    """Instance side of a stub: accepts any construction, attribute, or call."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return make_stub(name)()

    def __call__(self, *args, **kwargs):
        return make_stub("result")()

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def __bool__(self):
        return True


def make_stub(name):
    """Build a fresh stub class usable as a base class, a constructor, or a flag."""
    return _StubMeta(name, (_Stub,), {})


class _StubModule(types.ModuleType):
    """Module whose every attribute materialises as a stub on first access."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        stub = make_stub(name)
        setattr(self, name, stub)
        return stub


class ActionPlugin(object):
    """
    Faithful stand-in for ``pcbnew.ActionPlugin``.

    Mirrors KiCad's contract: ``__init__`` seeds the toolbar attributes and calls
    ``defaults()``, and ``register()`` publishes the instance. Registrations land
    in :data:`REGISTERED` so a test can assert *which* plugin appeared -- the
    real one, or the bootstrap's failure placeholder.
    """

    def __init__(self):
        self.icon_file_name = ""
        self.dark_icon_file_name = ""
        self.show_toolbar_button = False
        self.defaults()

    def defaults(self):
        self.name = "Undefined Action plugin"
        self.category = "Undefined"
        self.description = ""

    def register(self):
        REGISTERED.append(self)


REGISTERED = []


def install():
    """
    Put the fake KiCad runtime into ``sys.modules``.

    Returns the fake ``pcbnew`` module. Importing ``plugins`` after this call
    runs the real bootstrap, because ``'pcbnew' in sys.modules`` is how it
    detects that it is inside KiCad.
    """
    del REGISTERED[:]

    wx_module = _StubModule("wx")
    sys.modules["wx"] = wx_module

    pcbnew_module = _StubModule("pcbnew")
    pcbnew_module.ActionPlugin = ActionPlugin
    pcbnew_module.GetBuildVersion = lambda: "stub-kicad"
    sys.modules["pcbnew"] = pcbnew_module
    return pcbnew_module


# Modules that must import cleanly on every interpreter KiForge supports.
# ``plugins`` is last: importing it runs the bootstrap and registers the plugin.
SHIPPED_MODULES = ("kiforge", "package_plugin", "plugins.kiforge_studio", "plugins")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_import_gate():
    """
    Import every shipped module under the fake runtime and confirm registration.

    Returns a list of failure strings; empty means the gate passed.
    """
    import importlib

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    install()
    failures = []
    for name in SHIPPED_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            import traceback
            failures.append("%s failed to import: %s\n%s" % (name, exc, traceback.format_exc()))

    names = [type(plugin).__name__ for plugin in REGISTERED]
    if "ExporterPlugin" not in names:
        failures.append(
            "KiForge did not register a toolbar button; registered=%s%s"
            % (names or "nothing", "".join("\n  %s" % p.description for p in REGISTERED))
        )
    return failures


def run_theme_checks():
    """
    Check the Studio palette invariants that a paint handler cannot report.

    Paint handlers swallow exceptions, so a key present in one ramp and missing
    from the other shows up as a silently unpainted control rather than an
    error. These run under the stub because they need no real wx: the values are
    opaque here, only the structure matters.
    """
    from plugins import kiforge_studio as studio

    failures = []

    dark, light = set(studio._DARK_PALETTE), set(studio._LIGHT_PALETTE)
    if dark != light:
        failures.append(
            "palette ramps disagree; dark-only=%s light-only=%s"
            % (sorted(dark - light) or "none", sorted(light - dark) or "none")
        )

    modes = ("dark", "light")
    for name in modes:
        if name not in studio._TAB_ICON_TINTS:
            failures.append("no tab icon tint defined for %r mode" % name)
        if name not in studio._MSG_ICON_COLORS_BY_MODE:
            failures.append("no severity colours defined for %r mode" % name)
    if len(set(studio._TAB_ICON_TINTS.values())) != len(modes):
        failures.append("tab icon tints do not differ between themes")

    severity = [set(studio._MSG_ICON_COLORS_BY_MODE[m]) for m in modes
                if m in studio._MSG_ICON_COLORS_BY_MODE]
    if severity and any(keys != severity[0] for keys in severity):
        failures.append("severity colour tables define different keys per theme")

    # The whole design rests on _COLORS being mutated rather than rebound:
    # ~100 lookups across the file read this exact dict object, and a rebind
    # would leave every one of them pointing at the old ramp.
    before = studio._COLORS
    mode = studio.refresh_palette()
    if mode not in modes:
        failures.append("refresh_palette returned %r, expected one of %s" % (mode, modes))
    if studio._COLORS is not before:
        failures.append("refresh_palette rebound _COLORS instead of updating it in place")
    if set(studio._COLORS) != dark:
        failures.append("active palette lost keys after refresh")
    return failures


def main():
    failures = run_import_gate()
    if not failures:
        failures = run_theme_checks()
    if failures:
        sys.stderr.write("KiCad import gate FAILED on Python %s\n\n" % sys.version.split()[0])
        for failure in failures:
            sys.stderr.write("%s\n" % failure)
        return 1
    plugin = [p for p in REGISTERED if type(p).__name__ == "ExporterPlugin"][0]
    print(
        "KiCad import gate passed on Python %s: registered %r (toolbar button: %s)"
        % (sys.version.split()[0], plugin.name, plugin.show_toolbar_button)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
