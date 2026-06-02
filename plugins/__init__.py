"""
KiForge plugin package for KiCad 10.
Registers the ExporterPlugin ActionPlugin when loaded inside the KiCad scripting environment.

KiCad loads this package via __init__.py when it scans the scripting/plugins directory.
'pcbnew' being present in sys.modules is the reliable signal that we are running inside KiCad.
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
