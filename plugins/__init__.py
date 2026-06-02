"""
KiForge plugin for KiCad 10
Automated manufacturing and documentation exports
"""

# Import the plugin to trigger its registration
try:
    from .kiforge_studio import ExporterPlugin
except ImportError:
    pass

__version__ = "0.1.0"
__author__ = "alphaseneca"
