"""
KiForge plugin package for KiCad 10.

Registers :class:`ExporterPlugin` (KiForge Studio) when loaded inside the KiCad
scripting environment. KiCad discovers this package via ``plugins/__init__.py``
when scanning the scripting/plugins directory.

The ``pcbnew`` module being present in ``sys.modules`` is the reliable signal
that Python is running inside KiCad — not during CLI, unit tests, or packaging.
"""
import sys

__version__ = "0.1.0"
__author__ = "alphaseneca"

# Only import and register when running inside the KiCad scripting environment.
# Avoids C++ assertions and ImportErrors in standalone/CLI/test contexts.
if 'pcbnew' in sys.modules:
    try:
        from .kiforge_studio import ExporterPlugin
        ExporterPlugin().register()
    except Exception as e:
        import logging
        logging.getLogger("KiForge").error(f"Failed to register KiForge plugin: {e}")
