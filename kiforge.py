#!/usr/bin/env python3
"""
KiForge — KiCad 10 Manufacturing & Documentation Exporter
==========================================================

Single source of truth for manufacturing and documentation exports. KiForge runs
``kicad-cli`` in a structured pipeline, produces both unedited KiCad BOM/placement
CSVs and optional JLC-ready copies via a built-in formatter, and can generate
GitHub/Gitea release workflows for downstream KiCad projects.

Entry points
------------
CLI (headless, GitHub Actions, Docker)::

    python kiforge.py [--project-path PATH] [--output-dir DIR] [--no-export-*]
    python kiforge.py --generate-cd [--project-path PATH] [--output-dir DIR]

Library (KiCad GUI plugin via ``kiforge_studio.py``)::

    context = ExportContext(project_path, output_dir_name, options, progress_callback)
    context.resolve()
    run_export(context=context)

Settings merge order
--------------------
Built-in defaults → global ``settings.json`` → project ``.kiforge.json`` → runtime
CLI/GUI flags. Saved JSON uses nested ``exports``, ``export_params``, and ``ibom``
groups; legacy flat keys and ``generate_ci`` are still accepted.

Configuration layers
--------------------
KiForge separates *what to export* from *how kicad-cli exports it* from *one-off run
behavior*. Only the middle layer (``export_params``) and export toggles are
user-configurable and flow through CLI, GitHub Action, CD YAML, and
``.kiforge.json``. BOM layout and 3D render flags are fixed constants.

1. **Export toggles** (``exports`` / ``EXPORT_SETTING_KEYS``) — which artifacts
   to produce (Gerbers, BOM, STEP, …).
2. **Export parameters** (``export_params`` / ``EXPORT_PARAM_SPECS``) — placement
   CSV and STEP ``kicad-cli`` flags. Declared once in :data:`EXPORT_PARAM_SPECS`;
   consumed by Studio, :func:`parse_cli_args`, ``action.yml`` → ``action/run.sh``,
   and :func:`build_cd_substitutions`.
3. **Runtime options** (``RUNTIME_OPTION_SPECS``) — per-run only (e.g.
   ``sync_title_block_rev``); not saved to ``.kiforge.json``.
4. **Fixed pipelines** — :data:`BOM_EXPORT_DEFAULTS` (raw BOM + iBOM columns/grouping),
   :data:`RENDER_3D_DEFAULTS` (3D PNG renders), :data:`GERBER_EXPORT_DEFAULTS` /
   :data:`DRILL_EXPORT_DEFAULTS` (JLC-aligned gerber/drill). Not exposed as parameters.

Symbol fields for assembly: put JLC/LCSC numbers in **ID** (e.g. ``C125111``); **MPN**
is exported in the raw BOM only. JLC ``LCSC Part #`` is derived from ``ID`` when it
matches ``^C\\d+$``.

See ``ARCHITECTURE.md`` §7 for the full configuration reference.

Version suffix (output filenames)
---------------------------------
Resolved by :func:`resolve_export_version` in priority order:

1. Explicit ``version`` option / ``--version-tag``
2. ``GITHUB_REF_NAME`` when running in GitHub Actions
3. ``VERSION`` environment variable
4. Latest git tag in the project directory
5. Default ``v0.1.0``

The suffix is appended to ``pcb_name`` (e.g. ``sample_v1.2.0``). Schematic PDF
export can sync ``(rev …)`` via a staged copy without modifying the source file.

Typical outputs (when all toggles enabled)
------------------------------------------
``{name}_gerbers.zip``, ``{name}_bom.csv``, ``{name}_bom_jlc.csv``,
``{name}_pos.csv``, ``{name}_cpl_jlc.csv``, ``{name}_sch.pdf``, ``{name}.step``,
3D renders, SVGs, ``{name}_ibom.html``. Raw BOM/POS are kept; JLC variants are
additional files when ``format_jlc`` is on.

Pipeline
--------
:class:`ExportRunner` executes :class:`ExportTask` subclasses in order: kicad-cli
exports first, then post-processing (Gerber zip, BOM/POS rename and JLC format).
Individual step failures produce warnings; the run continues unless every step fails
or the user cancels.

Interactive HTML BOM
--------------------
``INTERACTIVE_HTML_BOM_*`` environment flags are set only in
:func:`ensure_ibom_subprocess_env` for the iBOM subprocess — never at import time,
or the standalone InteractiveHtmlBom plugin will not register its toolbar in KiCad.

Key types
---------
PathResolver     Resolve ``kicad-cli`` and KiCad Python across platforms.
ExportContext    Resolved paths, options, subprocess env, cancellation, offsets.
JLCPCBFormatter  Formats JLC-upload BOM/CPL from KiCad CSV exports.
ExportTask       Abstract export step; subclasses implement ``is_applicable`` / ``run``.
ExportRunner     Ordered pipeline driver with progress and cleanup.
generate_cd_files  Write CD workflow YAML and update project ``.gitignore``.
"""

import os
import sys
import csv
import html
import io
import zipfile
import shutil
import tempfile
import subprocess
import logging
import site
import threading
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable

# Ensure the user's local site-packages folder is in sys.path
# This is critical for KiCad's isolated Python environment to recognize --user pip packages.
if hasattr(site, 'getusersitepackages'):
    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.append(user_site)

# iBOM CLI flags — see module docstring; set only in ensure_ibom_subprocess_env().


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(output_dir=None):
    """
    Configure the KiForge logger for console and optional file output.

    Console logs go to stderr at INFO; when ``output_dir`` is set, DEBUG logs
    are also written to ``{output_dir}/kiforge.log``. Re-calling with a new
    ``output_dir`` replaces any existing file handler.
    """
    logger = logging.getLogger("KiForge")
    logger.setLevel(logging.DEBUG)
    
    # Remove existing FileHandlers if the output_dir is specified (so we can redirect to the new path)
    if output_dir:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
                handler.close()
    
    # Check if console and file handlers already exist
    has_console = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers)
    has_file = any(isinstance(h, logging.FileHandler) for h in logger.handlers)
    
    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(name)s:%(filename)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    if not has_console:
        # Console handler (outputs to KiCad scripting console / stderr)
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
    # File handler
    if output_dir and not has_file:
        try:
            os.makedirs(output_dir, exist_ok=True)
            log_file = os.path.join(output_dir, "kiforge.log")
            file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            # Fallback if log directory is unwritable
            logging.warning(f"Could not create log file: {e}")
            
    return logger

logger = logging.getLogger("KiForge.Core")

KIFORGE_ROOT = os.path.dirname(os.path.abspath(__file__))

# Default composite-action ref for CD workflows generated from a dev/git checkout.
# Release plugin zips pin this to alphaseneca/kiforge@<tag> at package time (see package_plugin.py).
KIFORGE_ACTION_REF = "alphaseneca/kiforge@main"

# InteractiveHtmlBom is a third-party package, not maintained by KiForge, with two
# unrelated install sites that must both stay pinned to this same version:
#   - Dockerfile: a mandatory, deterministic build-time dependency for the CD Action
#     image -- always installed, every build, never conditional.
#   - InteractiveBomTask (below): a local-machine-only convenience install, run just
#     once if a user's own KiCad Python environment doesn't already have it. This
#     path is not expected to run at all in the CD Action -- the Docker image has it
#     baked in already.
# Unpinned, a breaking or compromised upstream release would silently change or break
# both. Bump deliberately after testing a newer version; keep the Dockerfile's own
# pin (InteractiveHtmlBom==...) in sync by hand -- it can't import this constant.
INTERACTIVE_HTML_BOM_PINNED_VERSION = "2.11.2"

# ---------------------------------------------------------------------------
# Defaults & persisted settings
#
# Configuration model (first principles)
# --------------------------------------
# - EXPORT_SETTING_KEYS: boolean toggles persisted under JSON key "exports".
# - EXPORT_PARAM_SPECS: metadata table for placement/STEP flags; keys stored in
#   "export_params" and flattened onto ExportContext.options at run time.
# - BOM_EXPORT_DEFAULTS / RENDER_3D_DEFAULTS: hardcoded kicad-cli arguments for
#   BOM CSV and 3D PNGs — intentionally NOT in export_params (no CLI/Action/CD).
# - GERBER_EXPORT_DEFAULTS / DRILL_EXPORT_DEFAULTS: JLCPCB-aligned gerber/drill
#   export (manufacturing layers only, merged drill file).
# - DEFAULT_IBOM_SETTINGS: HTML BOM presentation only (tracks, dark mode, …);
#   column layout comes from BOM_EXPORT_DEFAULTS via build_ibom_cli_args().
# - RUNTIME_OPTION_SPECS: per-run flags (title-block sync); never saved.
#
# Merge order at export time: defaults → global settings.json → .kiforge.json →
# explicit CLI/GUI overrides (see load_merged_settings, build_cli_options).
# ---------------------------------------------------------------------------

DEFAULT_EXPORT_SETTINGS = {
    "export_gerbers": True,
    "export_drills": True,
    "export_pos": True,
    "export_bom": True,
    "export_ibom": True,
    "export_sch_pdf": True,
    "export_step": True,
    "export_3d": True,
    "export_svg": True,
    "export_print_pdf": True,
    "format_jlc": True,
    "generate_cd": True,
}

EXPORT_SETTING_KEYS = tuple(DEFAULT_EXPORT_SETTINGS.keys())

# Placement + STEP kicad-cli flags (persisted, configurable, CD/CLI/Action parity).
DEFAULT_EXPORT_PARAMS = {
    "pos_side": "both",
    "pos_smd_only": True,
    "pos_exclude_dnp": True,
    "step_subst_models": True,
    "bom_include_mfr_mpn": True,       # include Manufacturer and MPN columns in BOM/iBOM
}

# Raw KiCad BOM CSV + InteractiveHtmlBom column/group layout.
# Field order rationale: identify → source → verify → metadata
#   1. Reference, Value, Footprint — locate, identify & verify physical footprint
#   2. Manufacturer, MPN           — sourcing pair (toggleable via export_params)
#   3. ID                          — supplier/LCSC code for procurement
#   4. Description                 — physical package & supplementary detail
#   5. ${QUANTITY}, ${DNP}         — assembly metadata (count & do-not-place)
BOM_EXPORT_DEFAULTS = {
    "fields": "Reference,Value,Footprint,Manufacturer,MPN,ID,Description,${QUANTITY},${DNP}",
    "group_by": "Value,Footprint,Manufacturer,MPN,ID,DNP",
    "ref_range_delimiter": "",
}


def resolve_bom_fields(export_params: dict | None = None) -> dict:
    """
    Return BOM fields/group_by strings with Manufacturer and MPN conditionally included.

    When ``bom_include_mfr_mpn`` is False in *export_params*,
    those columns are stripped from both the field list and the group-by list.
    Returns a dict with ``fields``, ``group_by``, and ``ref_range_delimiter`` keys.
    """
    params = export_params or {}
    include_sourcing = params.get("bom_include_mfr_mpn", DEFAULT_EXPORT_PARAMS["bom_include_mfr_mpn"])

    exclude = set()
    if not include_sourcing:
        exclude.add("Manufacturer")
        exclude.add("MPN")

    def _filter(csv_str: str) -> str:
        return ",".join(t.strip() for t in csv_str.split(",") if t.strip() not in exclude)

    return {
        "fields": _filter(BOM_EXPORT_DEFAULTS["fields"]),
        "group_by": _filter(BOM_EXPORT_DEFAULTS["group_by"]),
        "ref_range_delimiter": BOM_EXPORT_DEFAULTS["ref_range_delimiter"],
    }

# 3D PNG render flags for kicad-cli pcb render (fixed).
RENDER_3D_DEFAULTS = {
    "zoom": 0.8,        # 80% zoom-out to add padding around the board edges
    "quality": "high",  # render quality: basic | high | user | job_settings
    "width": 2560,      # output image width in pixels (2K QHD)
    "height": 1440,     # output image height in pixels (2K QHD)
    "preset": "2",      # appearance preset: 0 = standard rasterizer, 2 = raytracing
}

# Gerber/drill export aligned with JLCPCB KiCad 9 guide (manufacturing layers only).
# kicad-cli defaults already match Protel extensions, X2, and netlist attributes when
# --no-protel-ext, --no-x2, and --no-netlist are omitted.
GERBER_EXPORT_DEFAULTS = {
    "check_zones": True,
    "use_drill_file_origin": True,
}

DRILL_EXPORT_DEFAULTS = {
    "format": "excellon",
    "drill_origin": "absolute",
    "excellon_units": "mm",
    "excellon_zeros_format": "decimal",
    "excellon_oval_format": "alternate",
}

# Non-manufacturing outputs to omit from the Gerber ZIP.
GERBER_ZIP_SKIP_SUFFIXES = (".gbrjob",)

_JLC_GERBER_TAIL_LAYERS = (
    "F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts",
    "Dwgs.User", "Cmts.User",
)

_JLC_GERBER_FALLBACK_LAYERS = (
    "F.Cu", "B.Cu", *_JLC_GERBER_TAIL_LAYERS,
)

# Registry: each entry maps one export_params key → CLI flag, Action input, CD placeholder.
EXPORT_PARAM_SPECS = (
    {
        "key": "pos_side",
        "type": "choice",
        "choices": ("both", "front", "back"),
        "cli": "--pos-side",
        "help": "Placement CSV: board side (both, front/top, back/bottom)",
        "action_input": "pos_side",
        "cd_placeholder": "POS_SIDE",
    },
    {
        "key": "pos_smd_only",
        "type": "bool",
        "cli": "--pos-smd-only",
        "help": "Placement CSV: include SMD parts only",
        "action_input": "pos_smd_only",
        "cd_placeholder": "POS_SMD_ONLY",
    },
    {
        "key": "pos_exclude_dnp",
        "type": "bool",
        "cli": "--pos-exclude-dnp",
        "help": "Placement CSV: exclude do-not-populate parts",
        "action_input": "pos_exclude_dnp",
        "cd_placeholder": "POS_EXCLUDE_DNP",
    },
    {
        "key": "step_subst_models",
        "type": "bool",
        "cli": "--step-subst-models",
        "help": "STEP export: substitute missing 3D models",
        "action_input": "step_subst_models",
        "cd_placeholder": "STEP_SUBST_MODELS",
    },
    {
        "key": "bom_include_mfr_mpn",
        "type": "bool",
        "cli": "--bom-include-mfr-mpn",
        "help": "BOM/iBOM: include Manufacturer and MPN columns",
        "action_input": "bom_include_mfr_mpn",
        "cd_placeholder": "BOM_INCLUDE_MFR_MPN",
    },
)

EXPORT_PARAM_KEYS = tuple(spec["key"] for spec in EXPORT_PARAM_SPECS)
assert set(EXPORT_PARAM_KEYS) == set(DEFAULT_EXPORT_PARAMS), "EXPORT_PARAM_SPECS keys must match DEFAULT_EXPORT_PARAMS"

# Per-run flags (CLI / Action / CD). Not loaded from or saved to .kiforge.json.
DEFAULT_EXPORT_RUNTIME_OPTIONS = {
    "sync_title_block_rev": True,
}

RUNTIME_OPTION_SPECS = (
    {
        "key": "sync_title_block_rev",
        "type": "bool",
        "cli": "--sync-title-block-rev",
        "help": "Sync schematic title-block (rev) to export version via staged copy",
        "action_input": "sync_title_block_rev",
        "cd_placeholder": "SYNC_TITLE_BLOCK_REV",
    },
)


def apply_export_runtime_options(options: dict | None) -> dict:
    """Attach per-run flags from RUNTIME_OPTION_SPECS (never loaded from JSON)."""
    merged = dict(options or {})
    for key, default in DEFAULT_EXPORT_RUNTIME_OPTIONS.items():
        merged.setdefault(key, default)
    return merged

DEFAULT_SETTINGS = {
    "output_dir": "kiforge",
    **DEFAULT_EXPORT_SETTINGS,
}

def get_project_settings_path(project_dir: str) -> str:
    """Return the path to a KiCad project's .kiforge.json settings file."""
    return os.path.join(project_dir, ".kiforge.json")


# ---------------------------------------------------------------------------
# Subprocess helpers (cancellation, Windows console hiding)
# ---------------------------------------------------------------------------

def _subprocess_startupinfo():
    """Hide console windows for git subprocess calls on Windows."""
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def _terminate_subprocess(proc: subprocess.Popen | None) -> None:
    """Stop a subprocess and its children without blocking indefinitely."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _communicate_with_cancel(
    proc: subprocess.Popen,
    context: "ExportContext",
    poll_interval: float = 0.1,
) -> tuple[str, str, int | None]:
    """Wait for a subprocess while honouring ExportContext cancellation."""
    while True:
        if context.is_aborted():
            _terminate_subprocess(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_subprocess(proc)
                stdout, stderr = proc.communicate(timeout=5)
            return stdout or "", stderr or "", proc.returncode

        try:
            stdout, stderr = proc.communicate(timeout=poll_interval)
            return stdout or "", stderr or "", proc.returncode
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------------------
# Version resolution & title-block staging
# ---------------------------------------------------------------------------

def resolve_git_latest_tag(search_dir: str) -> str | None:
    """
    Return the most recent git tag for a directory (matches CD release naming locally).

    Tries ``git describe --tags --abbrev=0`` first (current checkout tag when on a tag,
    otherwise nearest ancestor tag), then falls back to the newest tag by version sort.
    """
    if not search_dir or not os.path.isdir(search_dir):
        return None
    git_exe = shutil.which("git")
    if not git_exe:
        return None
    startupinfo = _subprocess_startupinfo()
    try:
        result = subprocess.run(
            [git_exe, "-C", search_dir, "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            timeout=10,
            startupinfo=startupinfo,
        )
        if result.returncode == 0:
            tag = result.stdout.strip()
            if tag:
                if tag.startswith("refs/tags/"):
                    tag = tag[len("refs/tags/"):]
                return tag
        result = subprocess.run(
            [git_exe, "-C", search_dir, "tag", "--sort=-v:refname"],
            capture_output=True,
            text=True,
            timeout=10,
            startupinfo=startupinfo,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                tag = line.strip()
                if tag:
                    return tag
    except Exception as exc:
        logger.debug(f"Git tag lookup failed for {search_dir}: {exc}")
    return None


# Characters allowed in a version suffix once it becomes part of output filenames.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename_component(value: str, fallback: str = "") -> str:
    """
    Reduce a string to a safe single path component for output filenames.

    Strips directory separators, drive letters, and other characters that could
    enable path traversal or produce invalid files. Version strings and board
    names can originate from git tags, ``GITHUB_REF_NAME``, or the filesystem,
    so they are treated as untrusted input.
    """
    if not value:
        return fallback
    # Take the final path component so a value like "a/b/../c" cannot escape.
    candidate = value.replace("\\", "/").split("/")[-1].strip()
    candidate = _UNSAFE_FILENAME_CHARS.sub("_", candidate)
    candidate = candidate.strip("._")
    return candidate or fallback


def normalize_version_suffix(version: str) -> str:
    """Normalize a version string for safe use in output filenames (e.g. v1.2.0)."""
    version_str = (version or "").strip()
    if "/" in version_str:
        version_str = version_str.split("/")[-1]
    version_str = sanitize_filename_component(version_str, fallback="0.1.0")
    if version_str and version_str[0].isdigit():
        version_str = f"v{version_str}"
    return version_str or "v0.1.0"


def apply_version_suffix(base_name: str, version_str: str) -> str:
    """Append a version suffix unless the board name is already versioned."""
    if re.search(r"_v[\w.\-]+$", base_name):
        return base_name
    suffix = normalize_version_suffix(version_str)
    return f"{base_name}_{suffix}"


def resolve_export_version(options: dict, project_dir: str) -> tuple[str, str]:
    """
    Resolve the version suffix used in output filenames.

    Priority: explicit ``version`` option → ``GITHUB_REF_NAME`` (CI tags) →
    ``VERSION`` env → latest git tag in the project → ``v0.1.0``.

    Returns:
        Tuple of (normalized version string, human-readable source label).
    """
    if options.get("version"):
        return normalize_version_suffix(options["version"]), "option"

    ref_type = os.environ.get("GITHUB_REF_TYPE", "")
    if ref_type == "tag" or not ref_type:
        ci_tag = os.environ.get("GITHUB_REF_NAME")
        if ci_tag:
            return normalize_version_suffix(ci_tag), "GITHUB_REF_NAME"

    env_version = os.environ.get("VERSION")
    if env_version:
        return normalize_version_suffix(env_version), "VERSION"

    git_tag = resolve_git_latest_tag(project_dir)
    if git_tag:
        return normalize_version_suffix(git_tag), "git tag"

    return normalize_version_suffix("0.1.0"), "default"


def title_block_rev_value(version_str: str) -> str:
    """Return the revision string written into KiCad title blocks."""
    return normalize_version_suffix(version_str)


def update_kicad_file_title_block_rev(content: str, rev: str) -> str:
    """
    Insert or update ``(rev "...")`` inside a ``(title_block ...)`` without touching other fields.

    If no title block exists, one is added after the ``(paper ...)`` line when present,
    otherwise near the top of the file.
    """
    import re

    rev_safe = rev.replace("\\", "\\\\").replace('"', '\\"')
    rev_line = f'(rev "{rev_safe}")'

    if "(title_block" in content:
        if re.search(r"\(rev\s+", content):
            return re.sub(r'\(rev\s+"[^"]*"', f'(rev "{rev_safe}"', content, count=1)
        return re.sub(
            r"\(title_block\s*\n",
            f"(title_block\n\t{rev_line}\n",
            content,
            count=1,
        )

    block = f'\n\t(title_block\n\t\t{rev_line}\n\t)\n'
    if re.search(r'^\s*\(paper\s+"[^"]*"\)\s*$', content, flags=re.MULTILINE):
        return re.sub(r'(\(paper\s+"[^"]*"\)\s*\n)', r"\1" + block, content, count=1)
    return re.sub(r"(\(kicad_(?:sch|pcb)\s*\n)", r"\1" + block, content, count=1)


def create_title_block_staged_copy(source_path: str, version_str: str) -> tuple[str, str]:
    """
    Copy a KiCad file with its title-block rev set to the resolved export version.

    Returns ``(temp_dir, staged_path)``. The caller must remove ``temp_dir`` when done.
    The original project file is never modified, so git stays clean.
    """
    temp_dir = tempfile.mkdtemp(prefix="kiforge_titleblock_")
    staged_path = os.path.join(temp_dir, os.path.basename(source_path))
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()
    updated = update_kicad_file_title_block_rev(content, title_block_rev_value(version_str))
    with open(staged_path, "w", encoding="utf-8") as f:
        f.write(updated)
    return temp_dir, staged_path


# ---------------------------------------------------------------------------
# Settings merge & persistence
# ---------------------------------------------------------------------------

def merge_export_settings(base: dict | None, overlay: dict | None) -> dict:
    """Merge export toggle dicts (overlay wins)."""
    merged = DEFAULT_EXPORT_SETTINGS.copy()
    if base:
        for key in EXPORT_SETTING_KEYS:
            if key in base:
                merged[key] = _coerce_setting_value(DEFAULT_EXPORT_SETTINGS[key], base[key])
    if overlay:
        for key in EXPORT_SETTING_KEYS:
            if key in overlay:
                merged[key] = _coerce_setting_value(DEFAULT_EXPORT_SETTINGS[key], overlay[key])
    return merged


def merge_export_params(base: dict | None, overlay: dict | None) -> dict:
    """
    Merge placement/STEP parameter dicts into a full export_params object.

    Starts from :data:`DEFAULT_EXPORT_PARAMS`, applies ``base`` (e.g. saved JSON),
    then ``overlay`` (CLI or Studio). Validates ``pos_side`` against allowed values.
    """
    merged = DEFAULT_EXPORT_PARAMS.copy()
    if base:
        for key in EXPORT_PARAM_KEYS:
            if key in base:
                merged[key] = _coerce_setting_value(DEFAULT_EXPORT_PARAMS[key], base[key])
    if overlay:
        for key in EXPORT_PARAM_KEYS:
            if key in overlay:
                merged[key] = _coerce_setting_value(DEFAULT_EXPORT_PARAMS[key], overlay[key])
    if merged.get("pos_side") not in ("both", "front", "back"):
        merged["pos_side"] = "both"
    return merged


def apply_export_params_to_options(options: dict) -> dict:
    """
    Flatten nested ``export_params`` onto the options dict used by ExportContext.

    Studio and save_settings store parameters under ``export_params``; export
    tasks read flat keys (``pos_side``, ``step_subst_models``, …) from
    ``context.options``. Does not touch BOM or render constants.
    """
    merged = dict(options)
    flat_overlay = {key: options[key] for key in EXPORT_PARAM_KEYS if key in options}
    params = merge_export_params(
        options.get("export_params") if isinstance(options.get("export_params"), dict) else None,
        flat_overlay or None,
    )
    merged.update(params)
    merged["export_params"] = params
    return merged


_STEP_EXPORT_WARNINGS = (
    "Cannot use VRML models",
    "non-mesh formats",
    "Cannot add a VRML model",
    "No solid model created",
    "Could not load model",
    "Failed to load",
    "3D model not found",
    "Error loading mesh",
)


# ---------------------------------------------------------------------------
# Templates, CD workflows, and gitignore
# ---------------------------------------------------------------------------

def get_kiforge_root() -> str:
    """Return the directory containing the installed kiforge.py module."""
    return KIFORGE_ROOT


def get_template_path(filename: str) -> str | None:
    """
    Find an editable template file that ships with KiForge.

    Resolution order:
      1. ``<kiforge.py dir>/templates/`` — repo root (CLI dev) or PCM install
         (``plugins/templates/`` inside the plugin zip)
      2. ``<parent of kiforge.py>/templates/`` — repo root when running the
         copied ``plugins/kiforge.py`` during local plugin development

    Edit files in the repo ``templates/`` directory only; packaging copies them
    into the plugin zip at build time.
    """
    candidates = [
        os.path.join(KIFORGE_ROOT, "templates", filename),
        os.path.join(os.path.dirname(KIFORGE_ROOT), "templates", filename),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def require_template_path(filename: str) -> str:
    """Return a template path or raise with an actionable error message."""
    path = get_template_path(filename)
    if not path:
        raise FileNotFoundError(
            f"KiForge template not found: {filename}. "
            f"Expected templates/{filename} beside kiforge.py "
            f"(see the templates/ directory in the KiForge source repository)."
        )
    return path


def _gitignore_pattern_from_line(line: str) -> str | None:
    """Extract a gitignore pattern from a template line, skipping comments."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "#" in stripped:
        hash_idx = stripped.index("#")
        if hash_idx == 0 or stripped[hash_idx - 1] != "\\":
            stripped = stripped[:hash_idx].rstrip()
    return stripped or None


def get_gitignore_template_path():
    """Return the path to templates/kiforge.gitignore when installed."""
    return get_template_path("kiforge.gitignore")


def _cd_option_str(options: dict, key: str, default: bool = True) -> str:
    return "true" if options.get(key, default) else "false"


def _export_toggle_cd_placeholder(export_key: str) -> str:
    """Map export toggle keys to CD template placeholders (export_3d → EXPORT_3D)."""
    return export_key.upper()


def _cd_value_for_export_param(options: dict, spec: dict) -> str:
    """Format one export_params value for CD workflow YAML substitution."""
    key = spec["key"]
    default = DEFAULT_EXPORT_PARAMS[key]
    if spec["type"] == "bool":
        return _cd_option_str(options, key, default)
    return str(options.get(key, default))


def _normalize_cd_options(options: dict) -> dict:
    """Flatten export toggles and export_params for CD template substitution."""
    return apply_export_runtime_options(apply_export_params_to_options(options or {}))


def build_cd_substitutions(output_dir_name: str, options: dict) -> dict[str, str]:
    """
    Build all ``{{PLACEHOLDER}}`` values for CD workflow templates.

    Derived from :data:`EXPORT_SETTING_KEYS`, :data:`EXPORT_PARAM_SPECS`, and
    :data:`RUNTIME_OPTION_SPECS` so Studio, CLI ``--generate-cd``, and the
    composite Action share one mapping table.
    """
    opts = _normalize_cd_options(options)
    substitutions = {
        "OUTPUT_DIR": output_dir_name,
        "KIFORGE_ACTION_REF": KIFORGE_ACTION_REF,
        "GITHUB_REF_NAME": "${{ github.ref_name }}",
    }
    for key in EXPORT_SETTING_KEYS:
        if key == "generate_cd":
            continue
        substitutions[_export_toggle_cd_placeholder(key)] = _cd_option_str(
            opts, key, DEFAULT_EXPORT_SETTINGS[key]
        )
    for spec in EXPORT_PARAM_SPECS:
        substitutions[spec["cd_placeholder"]] = _cd_value_for_export_param(opts, spec)
    for spec in RUNTIME_OPTION_SPECS:
        placeholder = spec.get("cd_placeholder")
        if placeholder:
            substitutions[placeholder] = _cd_option_str(
                opts, spec["key"], DEFAULT_EXPORT_RUNTIME_OPTIONS[spec["key"]]
            )
    return substitutions


def render_cd_workflow_template(template_name: str, output_dir_name: str, options: dict) -> str:
    """
    Load a CD workflow YAML template and substitute export options.

    Template placeholders use {{NAME}} syntax (see templates/github-release.yml).
    """
    template_path = require_template_path(template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    substitutions = build_cd_substitutions(output_dir_name, options)
    for key, value in substitutions.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    return content


def get_global_settings_path() -> str:
    """Return the path to the user-wide KiForge settings file."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "kiforge", "settings.json")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "kiforge", "settings.json"
        )
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(xdg, "kiforge", "settings.json")


# ---------------------------------------------------------------------------
# Studio tab icons (wx-free — cache, CDN fetch, SVG tint for dark UI)
# ---------------------------------------------------------------------------

TAB_ICON_CDN = {
    "export": "file_download",
    "advanced": "tune",
    "releases": "label",
    # Message-dialog severity icons (see plugins/kiforge_studio.py's
    # _KiForgeMessageDialog). Shares this same fetch/cache/tint pipeline —
    # keyed with a "msg_" prefix so they never collide with the tab names above.
    "msg_success": "check_circle",
    "msg_error": "error",
    "msg_warning": "warning",
    "msg_cancelled": "cancel",
    "msg_info": "info",
    "msg_question": "help",
}
TAB_ICON_CDN_URL = (
    "https://fonts.gstatic.com/s/i/short-term/release/"
    "materialsymbolsoutlined/{icon}/default/24px.svg"
)
TAB_ICON_TINT_COLOUR = "#e4e4e7"


def tab_icon_cache_dir() -> str:
    path = os.path.join(os.path.dirname(get_global_settings_path()), "icon_cache")
    os.makedirs(path, exist_ok=True)
    return path


def _tab_icon_cache_path(tab_name: str) -> str:
    return os.path.join(tab_icon_cache_dir(), f"{tab_name}.svg")


def read_cached_tab_icon_svg(tab_name: str) -> bytes | None:
    path = _tab_icon_cache_path(tab_name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        return data if data.strip().startswith(b"<svg") else None
    except OSError as exc:
        logger.warning("Failed to read cached tab icon %s: %s", tab_name, exc)
        return None


def write_cached_tab_icon_svg(tab_name: str, data: bytes) -> None:
    if not data.strip().startswith(b"<svg"):
        return
    try:
        with open(_tab_icon_cache_path(tab_name), "wb") as handle:
            handle.write(data)
    except OSError as exc:
        logger.warning("Failed to cache tab icon %s: %s", tab_name, exc)


def download_tab_icon_svg(tab_name: str) -> bytes | None:
    icon_id = TAB_ICON_CDN.get(tab_name)
    if not icon_id:
        return None
    url = TAB_ICON_CDN_URL.format(icon=icon_id)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "KiForge-Studio/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = response.read()
        if data.strip().startswith(b"<svg"):
            write_cached_tab_icon_svg(tab_name, data)
            return data
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning("Tab icon CDN fetch failed for %s: %s", tab_name, exc)
    return None


def fetch_tab_icon_svg(tab_name: str) -> bytes | None:
    """Return tab icon SVG from disk cache, refreshing from CDN when missing."""
    cached = read_cached_tab_icon_svg(tab_name)
    if cached:
        return cached
    return download_tab_icon_svg(tab_name)


def prepare_tab_icon_svg(svg_data: bytes, colour: str = TAB_ICON_TINT_COLOUR) -> bytes:
    """
    Tint a Material Symbol SVG with an explicit fill colour.

    CDN SVGs omit ``fill``; wx rasterizes them as black on dark backgrounds.
    Defaults to the tab-icon tint; callers with their own colour (e.g. the
    themed message dialog's per-severity icon colours) pass it explicitly.
    """
    try:
        text = svg_data.decode("utf-8")
    except UnicodeDecodeError:
        return svg_data
    text = text.replace("currentColor", colour)
    if re.search(r"<path[^>]*\sfill=", text, re.I):
        text = re.sub(
            r'(<path[^>]*\s)fill="[^"]*"',
            rf'\1fill="{colour}"',
            text,
            flags=re.I,
        )
    else:
        text = re.sub(r"<path\s+", f'<path fill="{colour}" ', text, flags=re.I)
    if 'fill="' not in text.split(">", 1)[0]:
        text = re.sub(r"<svg\s+", f'<svg fill="{colour}" ', text, count=1)
    return text.encode("utf-8")


def _coerce_setting_value(default, value):
    if isinstance(default, bool) and isinstance(value, str):
        return value.lower() == "true"
    return value


# ---------------------------------------------------------------------------
# iBOM (Interactive HTML BOM) integration
#
# Presentation flags (tracks, dark mode, …) live in DEFAULT_IBOM_SETTINGS / ibom JSON.
# BOM column order and grouping mirror BOM_EXPORT_DEFAULTS so the HTML table matches
# the raw KiCad CSV. Custom symbol fields (ID, MPN) are passed via --extra-fields;
# missing fields export as empty columns without error.
# ---------------------------------------------------------------------------

_KICAD_TO_IBOM_FIELD_NAMES = {
    "Reference": "References",
    "${QUANTITY}": "Quantity",
    "${DNP}": "DNP",
}


def ibom_show_fields_from_bom_fields(bom_fields: str) -> str:
    """Map kicad-cli BOM field names to InteractiveHtmlBom column names."""
    columns = []
    for token in bom_fields.split(","):
        token = token.strip()
        if token:
            columns.append(_KICAD_TO_IBOM_FIELD_NAMES.get(token, token))
    return ",".join(columns)


def ibom_group_fields_from_bom_group_by(bom_group_by: str) -> str:
    """Map kicad-cli BOM group-by tokens to InteractiveHtmlBom group fields."""
    columns = []
    for token in bom_group_by.split(","):
        token = token.strip()
        if token:
            columns.append(_KICAD_TO_IBOM_FIELD_NAMES.get(token, token))
    return ",".join(columns)


def ibom_extra_fields_from_bom_fields(bom_fields: str) -> str:
    """Return custom schematic fields iBOM should load via --extra-fields."""
    builtin = {
        "Reference", "References", "Value", "Footprint",
        "${QUANTITY}", "Quantity",
    }
    extra = []
    for token in bom_fields.split(","):
        token = token.strip()
        mapped = _KICAD_TO_IBOM_FIELD_NAMES.get(token, token)
        if token and token not in builtin and mapped not in builtin:
            if mapped not in extra:
                extra.append(mapped)
    return ",".join(extra)


def build_ibom_cli_args(
    ibom_settings: dict | None,
    output_dir: str,
    extra_data_file: str | None = None,
    export_params: dict | None = None,
) -> list[str]:
    """
    Build InteractiveHtmlBom CLI argv.

    Always includes copper tracks and netlist information, maps kicad-cli field
    names to iBOM names, and passes --no-browser for unattended export.
    """
    args = [
        "--include-tracks",
        "--include-nets",
    ]
    resolved = resolve_bom_fields(export_params)
    show_fields = ibom_show_fields_from_bom_fields(resolved["fields"])
    group_fields = ibom_group_fields_from_bom_group_by(resolved["group_by"])
    if show_fields:
        args.extend(["--show-fields", show_fields])
    if group_fields:
        args.extend(["--group-fields", group_fields])
    extra_fields = ibom_extra_fields_from_bom_fields(resolved["fields"])
    if extra_fields:
        args.extend(["--extra-fields", extra_fields])
    if extra_data_file:
        args.extend(["--extra-data-file", extra_data_file])
    args.append("--no-browser")
    args.extend(["--dest-dir", output_dir])
    return args


def cleanup_partial_ibom_output(output_dir: str, pcb_name: str | None = None) -> None:
    """Remove iBOM HTML left behind when export is cancelled or interrupted."""
    if not output_dir:
        return
    candidates = [os.path.join(output_dir, "ibom.html")]
    if pcb_name:
        candidates.append(os.path.join(output_dir, f"{pcb_name}_ibom.html"))
    for path in candidates:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def build_ibom_subprocess_command(python_executable: str) -> list[str]:
    """
    Build the InteractiveHtmlBom CLI invocation for a subprocess.

    Uses runpy.run_module to run under the __main__ context (matching -m behavior)
    while allowing us to call wx.DisableAsserts() beforehand to suppress blocking C++
    wxWidgets debug dialogs/alerts in debug/assertion-enabled builds of KiCad.
    """
    code = (
        "import sys\n"
        "try:\n"
        "    import wx\n"
        "    wx.DisableAsserts()\n"
        "except Exception:\n"
        "    pass\n"
        "import runpy\n"
        "runpy.run_module('InteractiveHtmlBom.generate_interactive_bom', run_name='__main__')"
    )
    return [python_executable, "-c", code]


def ensure_ibom_subprocess_env(env: dict | None) -> dict:
    """Return env with InteractiveHtmlBom CLI/subprocess flags set."""
    merged = (env or os.environ).copy()
    merged["INTERACTIVE_HTML_BOM_NO_DISPLAY"] = "1"
    merged["INTERACTIVE_HTML_BOM_CLI_MODE"] = "1"
    return merged


def format_ibom_failure_message(stderr: str = "", stdout: str = "") -> str:
    """Turn InteractiveHtmlBom subprocess output into a short user-facing warning."""
    combined = f"{stderr or ''}\n{stdout or ''}"
    error_lines = [
        line.strip()
        for line in combined.splitlines()
        if re.search(r"\bERROR\b", line, re.IGNORECASE)
    ]
    text_lower = combined.lower()
    if "pcb outline" in text_lower or "edges layer" in text_lower or "edge.cuts" in text_lower:
        return (
            "Interactive HTML BOM was skipped: no board outline was found on Edge.Cuts. "
            "Draw the PCB outline on the Edge.Cuts layer, then run export again."
        )
    for line in reversed(error_lines):
        match = re.search(r"ERROR\s+(.*)$", line, re.IGNORECASE)
        if match:
            detail = match.group(1).strip().rstrip(".")
            if detail and detail.lower() != "parsing failed":
                return f"Interactive HTML BOM was skipped: {detail}."
    return (
        "Interactive HTML BOM was skipped due to an iBOM error. "
        "Other exports were still generated."
    )


def _summarize_subprocess_output(stderr: str = "", stdout: str = "", max_lines: int = 2) -> str:
    """Extract short, user-facing error lines from tool output."""
    collected: list[str] = []
    for block in (stderr or "", stdout or ""):
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(
                r"^\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[,\.\d]*\s+\w+\s+",
                "",
                line,
            )
            lower = line.lower()
            if any(token in lower for token in ("error", "failed", "fatal", "cannot", "unable", "not found")):
                collected.append(line)
    if not collected:
        for line in reversed((stderr or "").splitlines()):
            if line.strip():
                collected.append(line.strip())
                break
    if not collected:
        for line in reversed((stdout or "").splitlines()):
            if line.strip():
                collected.append(line.strip())
                break
    summary = "\n".join(collected[:max_lines]).strip()
    if len(summary) > 240:
        summary = summary[:237] + "..."
    return summary


def format_task_failure_message(
    task_name: str,
    stderr: str = "",
    stdout: str = "",
    cmd: list | None = None,
) -> str:
    """Format a failed export step for dialogs and CLI output."""
    if cmd and "InteractiveHtmlBom" in " ".join(cmd):
        return format_ibom_failure_message(stderr, stdout)
    detail = _summarize_subprocess_output(stderr, stdout)
    label = task_name.rstrip(".")
    if detail:
        first_line = detail.splitlines()[0]
        return f"{label} failed: {first_line}"
    return f"{label} failed. See the KiForge log for details."


def stage_ibom_project_copy(context: "ExportContext") -> tuple[str, str]:
    """
    Copy the PCB and project schematic files to a temporary staging folder.

    Avoids pcbnew lock file conflicts when open in KiCad GUI and allows iBOM
    to extract schematic extra fields (Description, DNP, ID, MPN) directly.
    """
    ibom_temp_dir = tempfile.mkdtemp(prefix="kiforge_ibom_")
    pcb_basename = os.path.basename(context.pcb_file)
    staged_pcb_path = os.path.join(ibom_temp_dir, pcb_basename)
    shutil.copy2(context.pcb_file, staged_pcb_path)

    if context.project_dir and os.path.isdir(context.project_dir):
        for f in os.listdir(context.project_dir):
            if f.endswith((".kicad_sch", ".kicad_pro", ".kicad_prl", ".net", ".xml")):
                src_f = os.path.join(context.project_dir, f)
                if os.path.isfile(src_f):
                    shutil.copy2(src_f, os.path.join(ibom_temp_dir, f))

    return ibom_temp_dir, staged_pcb_path


def _load_settings_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_rotation_offsets(value) -> dict:
    """Normalize rotation_offsets from saved JSON (dict or JSON-encoded string)."""
    if isinstance(value, dict):
        return value.copy()
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed.copy() if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _apply_settings_layer(settings: dict, loaded: dict) -> None:
    """Merge one settings file (global or project) into the cumulative settings dict."""
    for key, default in DEFAULT_SETTINGS.items():
        if key in loaded:
            settings[key] = _coerce_setting_value(default, loaded[key])
        elif key == "generate_cd" and "generate_ci" in loaded:
            settings[key] = _coerce_setting_value(default, loaded["generate_ci"])

    settings["exports"] = merge_export_settings(
        {k: settings[k] for k in EXPORT_SETTING_KEYS},
        loaded.get("exports"),
    )
    for key in EXPORT_SETTING_KEYS:
        settings[key] = settings["exports"][key]

    settings["export_params"] = merge_export_params(
        settings.get("export_params"),
        loaded.get("export_params"),
    )
    for key in EXPORT_PARAM_KEYS:
        settings[key] = settings["export_params"][key]

    if "rotation_offsets" in loaded:
        settings["rotation_offsets"] = _parse_rotation_offsets(loaded["rotation_offsets"])


def load_merged_settings(project_dir=None):
    """
    Build the effective KiForge settings for a run by layering configuration sources.

    Defaults are applied first, then the user-wide settings file (for example
    ~/.config/kiforge/settings.json on Linux), and finally project-local
    .kiforge.json when project_dir is given. Project values override global ones.
    Legacy generate_ci keys in saved JSON are still accepted as generate_cd.

    Args:
        project_dir: Optional KiCad project root containing .kiforge.json.

    Returns:
        dict: Merged settings ready for the GUI or ExportContext.
    """
    settings = DEFAULT_SETTINGS.copy()
    settings["exports"] = DEFAULT_EXPORT_SETTINGS.copy()
    settings["export_params"] = DEFAULT_EXPORT_PARAMS.copy()
    settings["rotation_offsets"] = {}

    global_path = get_global_settings_path()
    if os.path.isfile(global_path):
        try:
            _apply_settings_layer(settings, _load_settings_file(global_path))
        except Exception as e:
            logger.warning(f"Failed to load global settings from {global_path}: {e}")

    if project_dir:
        settings_file = get_project_settings_path(project_dir)
        if os.path.isfile(settings_file):
            try:
                _apply_settings_layer(settings, _load_settings_file(settings_file))
            except Exception as e:
                logger.warning(f"Failed to load project settings from {settings_file}: {e}")

    return settings


def save_settings(settings, project_dir=None, scope="project"):
    """
    Write KiForge settings to disk for reuse across sessions.

    Use scope="project" to save .kiforge.json beside the KiCad project, or
    scope="global" to save the user-wide defaults file returned by
    ``get_global_settings_path()``. Export toggles are stored under ``exports``;
    placement/STEP flags under ``export_params``; iBOM presentation under ``ibom``.

    Args:
        settings: Current option values (typically from the Studio dialog).
        project_dir: Required when scope is "project".
        scope: Either "project" or "global".

    Returns:
        str: Path of the file that was written.

    Raises:
        ValueError: If scope is "project" but project_dir is missing or invalid.
    """
    flat_exports = {key: settings.get(key, DEFAULT_EXPORT_SETTINGS[key]) for key in EXPORT_SETTING_KEYS}
    exports = merge_export_settings(flat_exports, settings.get("exports"))
    payload = {
        "output_dir": settings.get("output_dir", DEFAULT_SETTINGS["output_dir"]),
        "exports": exports,
        "export_params": merge_export_params(None, settings.get("export_params")),
    }

    if scope == "global":
        target = get_global_settings_path()
        os.makedirs(os.path.dirname(target), exist_ok=True)
    else:
        if not project_dir or not os.path.isdir(project_dir):
            raise ValueError("A valid project directory is required for project-scoped settings.")
        target = get_project_settings_path(project_dir)

    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4)
    return target


def load_gitignore_patterns(output_dir_name: str) -> list[str]:
    """
    Collect the gitignore patterns KiForge should ensure exist in a KiCad project.

    The output directory name (for example kiforge/) is always included first.
    Remaining patterns are read from templates/kiforge.gitignore.
    """
    patterns = [f"{output_dir_name}/"]
    template_path = require_template_path("kiforge.gitignore")
    with open(template_path, "r", encoding="utf-8") as f:
        for line in f:
            pattern = _gitignore_pattern_from_line(line)
            if pattern:
                patterns.append(pattern)
    return patterns


def update_project_gitignore(project_dir: str, output_dir_name: str) -> bool:
    """
    Keep a KiCad project's .gitignore in sync with KiForge and KiCad 10 artifacts.

    Called automatically after a successful export (when generate_cd is enabled)
    and when CD release workflow files are generated from the Studio dialog or CLI.
    Patterns come from load_gitignore_patterns(), which reads the editable template
    beside the installed kiforge.py when present.

    If .gitignore already exists, only patterns that are not yet listed are appended.
    If it does not exist, a new file is created with a KiForge output header plus the
    full template body from templates/kiforge.gitignore.

    Args:
        project_dir: KiCad project root that should receive or update .gitignore.
        output_dir_name: Export folder name to ignore (for example kiforge/).

    Returns:
        bool: True if the file was created or changed; False if every pattern was
              already present.
    """
    gitignore_path = os.path.join(project_dir, ".gitignore")
    target_ignores = load_gitignore_patterns(output_dir_name)
    gitignore_updated = False

    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = [line.strip() for line in content.splitlines()]
        missing = [
            item for item in target_ignores
            if item not in lines and f"/{item}" not in lines and f"./{item}" not in lines
        ]
        if missing:
            with open(gitignore_path, "a", encoding="utf-8") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write("\n# KiCad & KiForge patterns added by KiForge\n")
                for item in missing:
                    f.write(f"{item}\n")
            gitignore_updated = True
    else:
        template_path = require_template_path("kiforge.gitignore")
        header = ["# KiForge output directory", f"{output_dir_name}/", ""]
        with open(template_path, "r", encoding="utf-8") as f:
            body = f.read().rstrip()
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write("\n".join(header) + body + "\n")
        gitignore_updated = True

    return gitignore_updated


def export_options_from_context(context: "ExportContext") -> dict:
    """
    Copy export and CD-related flags from a resolved ExportContext into a plain dict.

    Used when generating CD workflow YAML after export so the release pipeline
    matches export toggles, export_params, and runtime options from the run.
    BOM and 3D render behavior is fixed inside KiForge and is not substituted
    into workflow YAML. Legacy ``generate_ci`` maps to ``generate_cd``.

    Args:
        context: A resolved ExportContext from the current run.

    Returns:
        dict: Option flags suitable for generate_cd_files().
    """
    keys = (
        "export_gerbers", "export_drills", "export_pos", "export_bom", "export_ibom",
        "export_sch_pdf", "export_step", "export_3d", "export_svg", "export_print_pdf", "format_jlc",
        "generate_cd", "version",
    )
    options = {key: context.options.get(key, DEFAULT_SETTINGS.get(key, True)) for key in keys}
    if "generate_cd" not in context.options and context.options.get("generate_ci") is not None:
        options["generate_cd"] = context.options.get("generate_ci")
    if isinstance(context.options.get("export_params"), dict):
        options["export_params"] = context.options["export_params"]
    return _normalize_cd_options(options)


def _build_subprocess_env(kicad_cli: str | None, project_dir: str | None = None) -> dict:
    """
    Build the environment passed to kicad-cli and helper subprocesses.

    Ensures user site-packages and KiCad PCM paths are on PYTHONPATH and prepends
    the KiCad bin directory to PATH. Configures KIPRJMOD and 3D model search path
    variables (KICAD10_3DMODEL_DIR, KISYS3DMOD) for embedded and project-local 3D assets.
    """
    env = os.environ.copy()
    python_paths: list[str] = []

    if hasattr(site, "getusersitepackages"):
        user_site = site.getusersitepackages()
        if user_site and os.path.exists(user_site):
            python_paths.append(user_site)

    for path in sys.path:
        if path and os.path.isdir(path) and ("3rdparty" in path.lower() or "site-packages" in path.lower()):
            if path not in python_paths:
                python_paths.append(path)

    if python_paths:
        existing_pp = env.get("PYTHONPATH", "")
        added_paths = os.pathsep.join(python_paths)
        env["PYTHONPATH"] = f"{added_paths}{os.pathsep}{existing_pp}" if existing_pp else added_paths

    if kicad_cli and os.path.isabs(kicad_cli):
        kicad_bin_dir = os.path.dirname(kicad_cli)
        path_env = env.get("PATH", "")
        env["PATH"] = f"{kicad_bin_dir}{os.pathsep}{path_env}" if path_env else kicad_bin_dir

    if project_dir and os.path.isdir(project_dir):
        abs_proj = os.path.abspath(project_dir)
        # KIPRJMOD is KiCad's own project-relative macro. It is the supported,
        # portable way for a project's footprints to reference 3D models it
        # ships itself (e.g. a model at "${KIPRJMOD}/3dmodels/part.step") and
        # works identically on every OS and CI environment.
        env["KIPRJMOD"] = abs_proj

        if "KICAD10_3DMODEL_DIR" not in env:
            # KICAD10_3DMODEL_DIR names KiCad's own official 3D library and
            # must never be pointed at a project's own folder instead:
            # standard footprints (resistors, caps, connectors, ...) resolve
            # their models relative to exactly this variable, so conflating
            # it with a project-local models directory silently breaks them
            # whenever no real system library is found. Project-bundled
            # models should use ${KIPRJMOD}-relative paths instead (above).
            system_3d_dir = _derive_system_3d_model_dir(kicad_cli)
            if system_3d_dir:
                env["KICAD10_3DMODEL_DIR"] = system_3d_dir

        resolved_3d = env.get("KICAD10_3DMODEL_DIR", "")
        if resolved_3d:
            for alias in ("KISYS3DMOD", "KICAD9_3DMODEL_DIR", "KICAD8_3DMODEL_DIR", "KICAD7_3DMODEL_DIR"):
                if alias not in env:
                    env[alias] = resolved_3d

    return env


def _derive_system_3d_model_dir(kicad_cli: str | None) -> str | None:
    """
    Locate KiCad's official 3D model library.

    Derives the path from the resolved ``kicad_cli`` binary's own install
    layout first, so it works for any install location or KiCad point release
    -- not just the version numbers hardcoded in the fallback list below,
    which only apply when ``kicad_cli`` has no usable directory to derive from
    (e.g. resolved purely from PATH).
    """
    candidates: list[str] = []
    if kicad_cli and os.path.isabs(kicad_cli):
        bin_dir = os.path.dirname(kicad_cli)
        if sys.platform == "darwin":
            # .../KiCad.app/Contents/MacOS/kicad-cli -> .../Contents/SharedSupport/3dmodels
            candidates.append(os.path.normpath(os.path.join(bin_dir, "..", "SharedSupport", "3dmodels")))
        else:
            # .../<prefix>/bin/kicad-cli[.exe] -> .../<prefix>/share/kicad/3dmodels
            candidates.append(os.path.normpath(os.path.join(bin_dir, "..", "share", "kicad", "3dmodels")))

    if sys.platform == "win32":
        candidates += [
            r"C:\Program Files\KiCad\10.0\share\kicad\3dmodels",
            r"C:\Program Files\KiCad\9.0\share\kicad\3dmodels",
            r"C:\Program Files\KiCad\8.0\share\kicad\3dmodels",
        ]
    elif sys.platform == "darwin":
        # KiCad's own documented default search paths on macOS (see the
        # KICAD*_3DMODEL_DIR default-path threads on forum.kicad.info): a
        # system-wide supplementary directory under Application Support, and
        # the library bundled inside the app itself. The official .dmg
        # installs the app bundle at this fixed path (not directly under
        # /Applications like most macOS apps); the SharedSupport candidate
        # mirrors the bin_dir-relative one derived above from kicad-cli's own
        # resolved location.
        candidates += [
            "/Library/Application Support/kicad/3dmodels",
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels",
        ]
    else:
        candidates += [
            "/usr/share/kicad/3dmodels",
            "/usr/local/share/kicad/3dmodels",
            "/usr/share/kicad/modules/packages3d",
        ]

    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


# ---------------------------------------------------------------------------
# KiCad executable resolution
# ---------------------------------------------------------------------------

class PathResolver:
    """
    Locate ``kicad-cli`` and the KiCad Python interpreter on Windows, macOS, and Linux.

    Search order: PATH, then platform-specific install directories. When running
    inside the KiCad GUI, :meth:`get_kicad_python_path` avoids using the KiCad
    application binary as Python (which would hang subprocess calls).
    """
    
    @staticmethod
    def get_kicad_cli_path() -> str:
        """Resolves the path to the kicad-cli executable, checking standard installation paths if not in PATH."""
        cli_path = shutil.which("kicad-cli")
        if cli_path:
            return cli_path
            
        if sys.platform == 'win32':
            candidates = [
                r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
                r"C:\Program Files\KiCad\8.0\bin\kicad-cli.exe",
            ]
            for path in candidates:
                if os.path.isfile(path):
                    return path
        elif sys.platform == 'darwin':
            path = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
            if os.path.isfile(path):
                return path
                
        return "kicad-cli"

    @staticmethod
    def get_kicad_python_path() -> str:
        """Resolves the python interpreter associated with KiCad (which has pcbnew)."""
        try:
            # pyrefly: ignore [missing-import]
            import pcbnew
            exe = sys.executable
            # Inside the KiCad GUI, sys.executable is the 'kicad' app binary, not a
            # Python interpreter — running '<kicad> -c ...' would launch the GUI and
            # hang forever. Use it only if it is actually python; otherwise derive the
            # real bundled interpreter from sys.prefix.
            if exe and os.path.basename(exe).lower().startswith("python"):
                return exe
            for name in ("python3", "python"):
                cand = os.path.join(sys.prefix, "bin", name)          # macOS/Linux
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
                cand_win = os.path.join(sys.prefix, name + ".exe")    # Windows
                if os.path.isfile(cand_win):
                    return cand_win
            # Fall through to the generic PATH / standard-dir search below.
        except ImportError:
            pass

        if sys.platform == 'win32':
            candidates = ['kicad-python.exe', 'python.exe', 'pythonw.exe']
        else:
            candidates = ['kicad-python', 'python3', 'python']

        # 2. Try to find relative to resolved kicad-cli path
        kicad_cli = PathResolver.get_kicad_cli_path()
        if kicad_cli and os.path.isabs(kicad_cli):
            cli_dir = os.path.dirname(kicad_cli)
            for name in candidates:
                path = os.path.join(cli_dir, name)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path

        # 3. Try standard installation directories
        if sys.platform == 'win32':
            dirs = [
                r"C:\Program Files\KiCad\10.0\bin",
                r"C:\Program Files\KiCad\9.0\bin",
                r"C:\Program Files\KiCad\8.0\bin",
            ]
            for d in dirs:
                for name in candidates:
                    path = os.path.join(d, name)
                    if os.path.isfile(path):
                        return path
        elif sys.platform == 'darwin':
            paths = [
                "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-python",
                "/Applications/KiCad/KiCad.app/Contents/MacOS/python3",
                "/Applications/KiCad/KiCad.app/Contents/MacOS/python",
            ]
            for path in paths:
                if os.path.isfile(path):
                    return path

        return sys.executable


# Backward-compatible aliases for tests and external callers.
def get_kicad_cli_path():
    """Return the resolved ``kicad-cli`` executable path."""
    return PathResolver.get_kicad_cli_path()


def get_kicad_python_path():
    """Return the KiCad Python interpreter (with ``pcbnew`` when inside KiCad)."""
    return PathResolver.get_kicad_python_path()


# ---------------------------------------------------------------------------
# Export context (one run's resolved state)
# ---------------------------------------------------------------------------

class ExportContext:
    """
    Resolved configuration and runtime state for a single export run.

    Call :meth:`resolve` before passing to :class:`ExportRunner`. Holds discovered
    project files, merged options, subprocess environment, version suffix,
    JLCPCB rotation offsets, and thread-safe cancellation for GUI exports.
    """
    
    def __init__(self, project_path: str, output_dir_name: str, options: dict, progress_callback=None):
        self.project_path = os.path.abspath(project_path)
        self.output_dir_name = output_dir_name
        self.options = options
        self.progress_callback = progress_callback
        
        # Resolved attributes
        self.kicad_cli = None
        self.kicad_python = None
        self.pcb_file = None
        self.sch_file = None
        self.pcb_name = None
        self.project_dir = None
        self.output_dir = None
        self.temp_gerber_dir = None
        self.env = None
        self.startupinfo = None
        self.logger = logger
        
        # Cancellation and thread-safety state
        self.active_process = None
        self._aborted = False
        self._lock = threading.Lock()
        self.rotation_offsets = {}
        self.version_str = None
        self.warnings: list[str] = []
        # Populated by SvgExportTask.run() when it plots the copper layers, so
        # HomebrewPdfExportTask can reuse that work instead of re-plotting and
        # re-merging the same sheet a second time. See export_copper_layers().
        self.homebrew_layers: dict | None = None
        self._step_index = 0
        self._step_total = 1

    def begin_step(self, index: int, total: int) -> None:
        """Record which pipeline step is running, for :meth:`report_progress`."""
        self._step_index = index
        self._step_total = max(1, total)

    def report_progress(self, fraction: float, message: str | None = None) -> None:
        """
        Report progress *within* the running task.

        The runner only reports once per task, so a task that takes a long
        time (the 1200 DPI homebrew PDF above all) leaves the bar parked on
        one value for its whole duration and reads as hung. ``fraction`` is
        0.0-1.0 through this task; it is mapped into that task's own slice of
        the overall bar, so sub-progress can never run backwards or overtake
        the next task.

        ``message`` is optional and usually omitted: the dialog is already
        showing this task's name, so most sub-steps only need to move the bar.
        Pass one only when it tells the user something the task name does not
        -- never to restate the step or to quote internals like DPI or which
        renderer was picked.
        """
        if not self.progress_callback:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        self.progress_callback(self._step_index + fraction, self._step_total, message)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal export issue to surface in the GUI after completion."""
        message = message.strip()
        if message and message not in self.warnings:
            self.warnings.append(message)
        self.logger.warning(message)

    def cancel(self):
        """Cancels the current export runner execution, terminating any active subprocess."""
        with self._lock:
            self._aborted = True
            if self.active_process:
                _terminate_subprocess(self.active_process)
                self.active_process = None

    def is_aborted(self) -> bool:
        """Checks if a cancellation request has been made."""
        with self._lock:
            return self._aborted

    def resolve(self) -> bool:
        """Resolve executables, project files, settings, version, and output paths."""
        self.kicad_cli = PathResolver.get_kicad_cli_path()
        self.kicad_python = PathResolver.get_kicad_python_path()
        self.startupinfo = _subprocess_startupinfo()

        if not self._discover_project_files():
            return False

        self.env = _build_subprocess_env(self.kicad_cli, self.project_dir)

        merged_settings = load_merged_settings(self.project_dir)
        self._merge_options_from_settings(merged_settings)

        version_str, version_source = resolve_export_version(self.options, self.project_dir)
        self.pcb_name = apply_version_suffix(self.pcb_name, version_str)
        self.version_str = version_str

        self._resolve_output_directories()
        self.rotation_offsets = _parse_rotation_offsets(merged_settings.get("rotation_offsets", {}))
        opt_offsets = self.options.get("rotation_offsets")
        if isinstance(opt_offsets, dict):
            self.rotation_offsets.update(_parse_rotation_offsets(opt_offsets))

        setup_logger(self.output_dir)
        self.logger.info(f"Resolved project: {self.pcb_name} in {self.project_dir}")
        self.logger.info(f"Version suffix: {version_str} (from {version_source})")
        self.logger.info(f"Target Output Directory: {self.output_dir}")
        self.logger.info(f"Resolved KiCad Python: {self.kicad_python}")
        return True

    def _discover_project_files(self) -> bool:
        """Walk the project tree to locate .kicad_pcb, .kicad_pro, and schematic files."""
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".history"]
            for file in files:
                if file.endswith(".kicad_pro") and not self.pcb_name:
                    self.pcb_name = os.path.splitext(file)[0]
                    self.project_dir = root
                elif file.endswith(".kicad_pcb") and not self.pcb_file:
                    self.pcb_file = os.path.join(root, file)

        if not self.pcb_file:
            self.logger.error("No KiCad board (.kicad_pcb) files found.")
            return False

        if not self.pcb_name:
            self.pcb_name = os.path.splitext(os.path.basename(self.pcb_file))[0]
            self.project_dir = os.path.dirname(self.pcb_file)

        sch_name = f"{self.pcb_name}.kicad_sch"
        potential_sch = os.path.join(self.project_dir, sch_name)
        if os.path.isfile(potential_sch):
            self.sch_file = potential_sch
        else:
            for root, dirs, files in os.walk(self.project_path):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d != ".history"]
                for file in files:
                    if file.endswith(".kicad_sch"):
                        self.sch_file = os.path.join(root, file)
                        break
                if self.sch_file:
                    break
        return True

    def _merge_options_from_settings(self, merged_settings: dict) -> None:
        """Apply saved global/project settings; explicit runtime options still win."""
        for key in DEFAULT_SETTINGS:
            if key not in self.options:
                self.options[key] = merged_settings[key]


        export_overlay = (
            self.options.get("exports") if isinstance(self.options.get("exports"), dict) else None
        )
        merged_exports = merge_export_settings(
            {k: self.options.get(k, merged_settings[k]) for k in EXPORT_SETTING_KEYS},
            export_overlay,
        )
        for key in EXPORT_SETTING_KEYS:
            self.options[key] = merged_exports[key]
        self.options["exports"] = merged_exports

        merged_params = merge_export_params(
            merged_settings.get("export_params"),
            self.options.get("export_params") if isinstance(self.options.get("export_params"), dict) else None,
        )
        for key in EXPORT_PARAM_KEYS:
            if key not in self.options:
                self.options[key] = merged_params[key]
        self.options["export_params"] = merged_params

    def _resolve_output_directories(self) -> None:
        """Create the versioned output folder and temporary gerber staging directory."""
        if os.path.isabs(self.output_dir_name):
            self.output_dir = self.output_dir_name
        else:
            self.output_dir = os.path.join(self.project_dir, self.output_dir_name)
        os.makedirs(self.output_dir, exist_ok=True)

        self.temp_gerber_dir = os.path.join(self.output_dir, "temp_gerbers")
        os.makedirs(self.temp_gerber_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# JLCPCB BOM/CPL formatting (KiCad Method 1 — kicad-cli export + column remap)
# https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad
# ---------------------------------------------------------------------------

# JLCPCB SMT assembly upload layout (BOM + centroid / CPL).
# Method 1 sample headers + Quantity (KiCad ${QUANTITY} / grouped ref count).
JLC_BOM_PART_COLUMN = "LCSC Part #"
JLC_BOM_COLUMNS = ("Comment", "Designator", "Footprint", JLC_BOM_PART_COLUMN, "Quantity")
JLC_CPL_COLUMNS = ("Designator", "Mid X", "Mid Y", "Rotation", "Layer")

# LCSC/JLC library part numbers use a leading C followed by digits (e.g. C125111).
LCSC_PART_ID_PATTERN = re.compile(r"^C\d+$")


class JLCPCBFormatter:
    """
    Build JLC-upload BOM/CPL CSVs from KiCad ``kicad-cli`` exports.

    BOM output columns: ``Comment``, ``Designator``, ``Footprint``,
    ``LCSC Part #``, ``Quantity``. CPL output columns: ``Designator``, ``Mid X``,
    ``Mid Y``, ``Rotation``, ``Layer`` (mm, Top/Bottom).

    Raw KiCad CSVs are unchanged. When ``format_jlc`` is on, writes
    ``*_bom_jlc.csv`` and ``*_cpl_jlc.csv`` beside ``*_bom.csv`` / ``*_pos.csv``.
    ``LCSC Part #`` is copied from symbol ``ID`` only when ``ID`` matches ``^C\\d+$``.
    """

    @staticmethod
    def _row_value(row: dict, *keys: str) -> str:
        for key in keys:
            value = row.get(key, "")
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _is_dnp(row: dict) -> bool:
        dnp = JLCPCBFormatter._row_value(row, "${DNP}", "DNP")
        return dnp.lower() in ("1", "dnp", "true", "yes")

    @staticmethod
    def _lcsc_part_number(row: dict) -> str:
        """Copy symbol ``ID`` into LCSC Part # only when it matches ``^C\\d+$``."""
        id_value = JLCPCBFormatter._row_value(row, "ID")
        if id_value and LCSC_PART_ID_PATTERN.match(id_value):
            return id_value
        return ""

    @staticmethod
    def _normalize_layer(side: str) -> str:
        normalized = side.strip().lower()
        if normalized in ("bottom", "back", "b.cu", "b", "bot"):
            return "Bottom"
        return "Top"

    @staticmethod
    def format_bom(raw_bom_path: str, output_bom_path: str) -> None:
        """Map KiCad BOM CSV columns to JLCPCB's upload format."""
        if not os.path.exists(raw_bom_path):
            return

        with open(raw_bom_path, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        jlc_rows = []
        for row in rows:
            if JLCPCBFormatter._is_dnp(row):
                continue

            designator = JLCPCBFormatter._row_value(row, "Reference", "Designator", "References")
            comment = JLCPCBFormatter._row_value(row, "Value", "Comment")
            footprint = JLCPCBFormatter._row_value(row, "Footprint")
            quantity = (
                JLCPCBFormatter._row_value(row, "${QUANTITY}", "QUANTITY", "Quantity", "Qty")
                or "1"
            )
            lcsc = JLCPCBFormatter._lcsc_part_number(row)

            jlc_rows.append({
                "Comment": comment,
                "Designator": designator,
                "Footprint": footprint,
                JLC_BOM_PART_COLUMN: lcsc,
                "Quantity": quantity,
            })

        with open(output_bom_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(JLC_BOM_COLUMNS))
            writer.writeheader()
            writer.writerows(jlc_rows)

    @staticmethod
    def format_cpl(raw_pos_path: str, output_cpl_path: str, rotation_offsets: dict = None) -> None:
        """Map KiCad placement CSV columns to JLCPCB centroid (CPL) format."""
        if not os.path.exists(raw_pos_path):
            return

        with open(raw_pos_path, "r", newline="", encoding="utf-8-sig") as handle:
            lines = handle.readlines()

        clean_lines = [line for line in lines if not line.strip().startswith("#")]
        rows = list(csv.DictReader(clean_lines))

        offsets = dict(rotation_offsets or {})
        jlc_cpl_rows = []
        for row in rows:
            designator = JLCPCBFormatter._row_value(row, "Ref", "Designator", "Reference")
            val = JLCPCBFormatter._row_value(row, "Val", "Value")
            package = JLCPCBFormatter._row_value(row, "Package", "Footprint")
            pos_x = JLCPCBFormatter._row_value(row, "PosX", "Mid X")
            pos_y = JLCPCBFormatter._row_value(row, "PosY", "Mid Y")
            rot_str = JLCPCBFormatter._row_value(row, "Rot", "Rotation")
            side = JLCPCBFormatter._row_value(row, "Side", "Layer")

            try:
                rotation = float(rot_str) if rot_str else 0.0
            except ValueError:
                rotation = 0.0

            for pattern, offset in offsets.items():
                needle = pattern.lower()
                if needle in package.lower() or needle in val.lower():
                    rotation = (rotation + offset) % 360.0
                    break

            rotation_text = (
                f"{rotation:.2f}" if rotation % 1 else str(int(rotation))
            )

            jlc_cpl_rows.append({
                "Designator": designator,
                "Mid X": pos_x,
                "Mid Y": pos_y,
                "Rotation": rotation_text,
                "Layer": JLCPCBFormatter._normalize_layer(side),
            })

        with open(output_cpl_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(JLC_CPL_COLUMNS))
            writer.writeheader()
            writer.writerows(jlc_cpl_rows)


def format_jlc_exports(context: "ExportContext") -> bool:
    """Build JLC-upload CSVs from versioned KiCad BOM/POS exports."""
    ok = True
    if context.options.get("export_bom", True):
        bom_path = os.path.join(context.output_dir, f"{context.pcb_name}_bom.csv")
        jlc_bom_path = os.path.join(context.output_dir, f"{context.pcb_name}_bom_jlc.csv")
        if os.path.isfile(bom_path):
            try:
                JLCPCBFormatter.format_bom(bom_path, jlc_bom_path)
                context.logger.info(
                    "Saved JLCPCB BOM: %s",
                    os.path.basename(jlc_bom_path),
                )
            except Exception as exc:
                context.logger.error("JLC BOM formatting failed: %s", exc, exc_info=True)
                context.add_warning(f"JLCPCB BOM formatting failed: {exc}")
                ok = False
        else:
            context.add_warning(f"KiCad BOM not found for JLC formatting: {bom_path}")
            ok = False
    if context.options.get("export_pos", True):
        pos_path = os.path.join(context.output_dir, f"{context.pcb_name}_pos.csv")
        jlc_cpl_path = os.path.join(context.output_dir, f"{context.pcb_name}_cpl_jlc.csv")
        if os.path.isfile(pos_path):
            try:
                JLCPCBFormatter.format_cpl(pos_path, jlc_cpl_path, context.rotation_offsets)
                context.logger.info(
                    "Saved JLCPCB CPL: %s",
                    os.path.basename(jlc_cpl_path),
                )
            except Exception as exc:
                context.logger.error("JLC CPL formatting failed: %s", exc, exc_info=True)
                context.add_warning(f"JLCPCB CPL formatting failed: {exc}")
                ok = False
        else:
            context.add_warning(f"KiCad placement file not found for JLC formatting: {pos_path}")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Export pipeline tasks
# ---------------------------------------------------------------------------

class ExportTask:
    """
    Abstract export step executed by :class:`ExportRunner`.

    Subclasses implement :meth:`is_applicable` (whether the step runs for the
    current options) and :meth:`run` (the work). Use :meth:`_run_subprocess` for
    external commands; pass ``env=ensure_ibom_subprocess_env(...)`` only for iBOM.
    """
    
    def __init__(self, name: str):
        self.name = name

    def is_applicable(self, context: ExportContext) -> bool:
        """Determines if the task should execute based on configuration context"""
        raise NotImplementedError

    def run(self, context: ExportContext) -> bool:
        """Executes the task's command or logic. Returns True if successful."""
        raise NotImplementedError

    def _run_subprocess(
        self,
        cmd: list,
        context: ExportContext,
        *,
        env=None,
        failure_message: str | None = None,
    ) -> bool:
        """Execute a subprocess; record a warning and return False instead of raising."""
        if context.is_aborted():
            return False

        run_env = context.env if env is None else env
        context.logger.info(f"Running command: {' '.join(cmd)}")
        try:
            with context._lock:
                if context._aborted:
                    return False
                context.active_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=context.project_dir,
                    env=run_env,
                    startupinfo=context.startupinfo,
                )

            stdout, stderr, returncode = _communicate_with_cancel(
                context.active_process, context
            )

            if stdout.strip():
                context.logger.debug(f"Command stdout:\n{stdout.strip()}")
            if stderr.strip():
                context.logger.debug(f"Command stderr:\n{stderr.strip()}")

            if context.is_aborted():
                return False

            if returncode not in (0, None):
                raise subprocess.CalledProcessError(
                    returncode, cmd, output=stdout, stderr=stderr
                )
            return True
        except subprocess.CalledProcessError as e:
            if context.is_aborted():
                return False
            err_output = (e.stderr or e.stdout or "").strip()
            context.logger.error(
                "Command failed (%s): %s\n%s",
                e.returncode,
                " ".join(cmd),
                err_output,
            )
            context.add_warning(
                failure_message
                or format_task_failure_message(self.name, e.stderr or "", e.stdout or "", cmd)
            )
            return False
        except OSError as e:
            if context.is_aborted():
                return False
            exe_name = cmd[0] if cmd else "unknown"
            context.logger.error("Failed to execute %s: %s", exe_name, e)
            context.add_warning(
                f"{self.name} failed: could not run '{exe_name}'. "
                f"Ensure KiCad is installed correctly."
            )
            return False
        except Exception as e:
            if context.is_aborted():
                return False
            context.logger.error(
                "Unexpected error running %s: %s", " ".join(cmd), e, exc_info=True
            )
            context.add_warning(f"{self.name} failed: {e}")
            return False
        finally:
            with context._lock:
                context.active_process = None


def _parse_pcb_layers(pcb_file: str) -> list[tuple[str, str]]:
    """Return (canonical_name, type) pairs from a board's ``(layers ...)`` section."""
    try:
        with open(pcb_file, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []
    match = re.search(r"\(layers\s+(.*?)\)\s*\(setup", content, re.DOTALL)
    if not match:
        return []
    return re.findall(r'\(\d+\s+"([^"]+)"\s+(\w+)', match.group(1))


def resolve_jlc_gerber_layers(pcb_file: str) -> list[str]:
    """
    JLCPCB manufacturing layers present on the board (KiCad 9 gerber guide).

    Copper: F.Cu, inner signal layers in stack order, B.Cu. Also paste, silk,
    mask, edge cuts, and user drawing/comment layers (Dwgs.User, Cmts.User).
    Other user, fab, and courtyard layers are omitted.
    """
    parsed = _parse_pcb_layers(pcb_file)
    if not parsed:
        return list(_JLC_GERBER_FALLBACK_LAYERS)

    names_present = {name for name, _ in parsed}
    result: list[str] = []
    inner: list[str] = []

    for name, layer_type in parsed:
        if name == "F.Cu":
            result.append(name)
        elif layer_type == "signal" and name not in ("F.Cu", "B.Cu"):
            inner.append(name)

    if "B.Cu" in names_present:
        result.extend(inner)
        result.append("B.Cu")
    elif inner:
        result.extend(inner)

    for layer in _JLC_GERBER_TAIL_LAYERS:
        if layer in names_present:
            result.append(layer)

    return result if result else list(_JLC_GERBER_FALLBACK_LAYERS)


def build_gerber_export_cmd(context: ExportContext) -> list[str]:
    """Build kicad-cli argv for JLC-aligned Gerber export."""
    layers = resolve_jlc_gerber_layers(context.pcb_file)
    cmd = [
        context.kicad_cli, "pcb", "export", "gerbers",
        "--layers", ",".join(layers),
    ]
    if GERBER_EXPORT_DEFAULTS.get("check_zones"):
        cmd.append("--check-zones")
    if GERBER_EXPORT_DEFAULTS.get("use_drill_file_origin"):
        cmd.append("--use-drill-file-origin")
    cmd.extend(["-o", context.temp_gerber_dir, context.pcb_file])
    return cmd


def build_drill_export_cmd(context: ExportContext) -> list[str]:
    """Build kicad-cli argv for JLC-aligned Excellon drill export."""
    d = DRILL_EXPORT_DEFAULTS
    return [
        context.kicad_cli, "pcb", "export", "drill",
        "--format", d["format"],
        "--drill-origin", d["drill_origin"],
        "--excellon-units", d["excellon_units"],
        "--excellon-zeros-format", d["excellon_zeros_format"],
        "--excellon-oval-format", d["excellon_oval_format"],
        "-o", context.temp_gerber_dir,
        context.pcb_file,
    ]


class GerberExportTask(ExportTask):
    """Export JLC manufacturing gerber layers to ``temp_gerbers/`` via kicad-cli."""

    def __init__(self):
        super().__init__("Exporting Gerber Layers")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_gerbers", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        return self._run_subprocess(build_gerber_export_cmd(context), context)


class DrillExportTask(ExportTask):
    """Export JLC-aligned Excellon drill files; runs when drills or gerbers are enabled."""
    def __init__(self):
        super().__init__("Exporting Drill Files")

    def is_applicable(self, context: ExportContext) -> bool:
        drills_requested = context.options.get("export_drills", True)
        gerbers_requested = context.options.get("export_gerbers", True)
        return (drills_requested or gerbers_requested) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        return self._run_subprocess(build_drill_export_cmd(context), context)


class PlacementExportTask(ExportTask):
    """
    Export KiCad placement CSV to ``raw_pos.csv``.

    Reads ``pos_side``, ``pos_smd_only``, and ``pos_exclude_dnp`` from
    ``context.options`` (from export_params). Always uses csv format, mm units,
    and drill-file origin — matching standard manufacturing scripts.
    """
    def __init__(self):
        super().__init__("Exporting Position Data")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_pos", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        raw_pos_path = os.path.join(context.output_dir, "raw_pos.csv")
        cmd = [
            context.kicad_cli, "pcb", "export", "pos",
            "--format", "csv",
            "--use-drill-file-origin",
            "--units", "mm",
        ]
        if context.options.get("pos_exclude_dnp", True):
            cmd.append("--exclude-dnp")
        if context.options.get("pos_smd_only", True):
            cmd.append("--smd-only")
        side = context.options.get("pos_side", "both")
        if side not in ("front", "back", "both"):
            side = "both"
        cmd.extend(["--side", side, context.pcb_file, "-o", raw_pos_path])
        return self._run_subprocess(cmd, context)


class BomExportTask(ExportTask):
    """
    Export KiCad BOM CSV to ``raw_bom.csv``.

    Uses fixed :data:`BOM_EXPORT_DEFAULTS` (fields, group-by, ref delimiter).
    Raw BOM includes symbol ``ID`` and ``MPN``; JLC-upload copies map ``ID`` to
    ``LCSC Part #`` when it matches ``^C\\d+$``. Missing symbol fields appear as
    empty columns.
    """
    def __init__(self):
        super().__init__("Exporting Bill of Materials")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_bom", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        raw_bom_path = os.path.join(context.output_dir, "raw_bom.csv")
        resolved = resolve_bom_fields(context.options)
        cmd = [
            context.kicad_cli, "sch", "export", "bom",
            "--fields", resolved["fields"],
            "--group-by", resolved["group_by"],
            "--ref-range-delimiter", resolved["ref_range_delimiter"],
            context.sch_file, "-o", raw_bom_path,
        ]
        return self._run_subprocess(cmd, context)


class SchematicPdfExportTask(ExportTask):
    """Export schematic to ``{pcb_name}_sch.pdf``; optional staged title-block rev sync."""
    def __init__(self):
        super().__init__("Exporting Schematic PDF")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_sch_pdf", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        output_pdf = os.path.join(context.output_dir, f"{context.pcb_name}_sch.pdf")
        sch_input = context.sch_file
        temp_dir = None
        try:
            if (
                context.options.get("sync_title_block_rev")
                and getattr(context, "version_str", None)
            ):
                try:
                    temp_dir, sch_input = create_title_block_staged_copy(
                        context.sch_file, context.version_str
                    )
                    context.logger.info(
                        f"Schematic PDF uses staged copy with title-block rev {context.version_str}"
                    )
                except Exception as exc:
                    context.logger.warning(
                        "Could not stage schematic for title-block sync (%s); using original file.",
                        exc,
                    )
            cmd = [
                context.kicad_cli, "sch", "export", "pdf",
                sch_input,
                "-o", output_pdf
            ]
            return self._run_subprocess(cmd, context)
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)


class Step3dExportTask(ExportTask):
    """
    Export ``{pcb_name}.step`` via kicad-cli.

    Honors ``step_subst_models`` from export_params. Always passes
    ``--no-optimize-step`` (fixed manufacturing default).
    Treats non-fatal KiCad model warnings as partial success when a STEP file exists.
    """
    def __init__(self):
        super().__init__("Exporting STEP 3D Model")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_step", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        """Export STEP; keep partial output when KiCad reports non-fatal model warnings."""
        output_step = os.path.join(context.output_dir, f"{context.pcb_name}.step")
        cmd = [context.kicad_cli, "pcb", "export", "step", "--no-optimize-step"]
        if context.options.get("step_subst_models", True):
            cmd.append("--subst-models")
        cmd.extend(["-f", "-o", output_step, context.pcb_file])
        if self._run_subprocess(cmd, context):
            return True
        if os.path.isfile(output_step) and os.path.getsize(output_step) > 0:
            context.add_warning(
                f"{self.name} finished with warnings; a partial STEP file was still saved."
            )
            return True
        return False


class Render3dExportTask(ExportTask):
    """
    Render front/back 3D PNGs using fixed :data:`RENDER_3D_DEFAULTS`.

    Features multi-stage rendering fallback ladder:
      1. Primary high-quality raytracing preset (--preset 2).
      2. Automatic fallback to standard rasterizer (--preset 0) if raytracing fails
         due to VRML (.wrl) mesh incompatibility, missing 3D models, or headless environment.
    """
    def __init__(self):
        super().__init__("Rendering 3D Views")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_3d", True) and bool(context.pcb_file)

    def _render_view(self, context: ExportContext, output_png: str, rotate_str: str, view_label: str) -> bool:
        primary_flags = [
            "--preset", RENDER_3D_DEFAULTS["preset"], "--floor",
            "--zoom", str(RENDER_3D_DEFAULTS["zoom"]),
            "--quality", RENDER_3D_DEFAULTS["quality"],
            "--width", str(RENDER_3D_DEFAULTS["width"]),
            "--height", str(RENDER_3D_DEFAULTS["height"]),
        ]
        cmd_primary = [
            context.kicad_cli, "pcb", "render", context.pcb_file,
            "--output", output_png,
            "--rotate", rotate_str,
            *primary_flags,
        ]
        if self._run_subprocess(cmd_primary, context):
            if os.path.isfile(output_png) and os.path.getsize(output_png) > 0:
                return True

        # A render that stopped because the user cancelled is not a failure
        # worth retrying -- the fallback below is another full render, and
        # run_command already returns False once the subprocess is terminated,
        # so without this the cancel silently bought a second long render.
        if context.is_aborted():
            return False

        context.logger.warning(
            "%s 3D raytracing render failed; attempting standard rasterizer fallback mode (--preset 0)...",
            view_label,
        )
        fallback_flags = [
            "--preset", "0", "--floor",
            "--zoom", str(RENDER_3D_DEFAULTS["zoom"]),
            "--quality", "normal",
            "--width", str(RENDER_3D_DEFAULTS["width"]),
            "--height", str(RENDER_3D_DEFAULTS["height"]),
        ]
        cmd_fallback = [
            context.kicad_cli, "pcb", "render", context.pcb_file,
            "--output", output_png,
            "--rotate", rotate_str,
            *fallback_flags,
        ]
        if self._run_subprocess(cmd_fallback, context):
            if os.path.isfile(output_png) and os.path.getsize(output_png) > 0:
                context.add_warning(
                    f"{view_label} 3D render used standard rasterizer fallback due to model/raytracing incompatibility."
                )
                return True

        if os.path.isfile(output_png) and os.path.getsize(output_png) > 0:
            context.add_warning(
                f"{view_label} 3D render completed with warnings; output PNG was saved."
            )
            return True

        return False

    def run(self, context: ExportContext) -> bool:
        front_png = os.path.join(context.output_dir, f"{context.pcb_name}_3d_front.png")
        ok_front = self._render_view(context, front_png, "0,0,0", "Front")
        if not ok_front:
            context.logger.warning("Front 3D render failed; attempting back view anyway.")

        # Front failing is explicitly not a reason to skip the back view, but
        # the user cancelling is: the back render is just as long as the front.
        if context.is_aborted():
            return ok_front

        back_png = os.path.join(context.output_dir, f"{context.pcb_name}_3d_back.png")
        ok_back = self._render_view(context, back_png, "0,180,0", "Back")
        return ok_front or ok_back


def parse_svg_dimensions(svg_path: str) -> tuple[float, float, float, float]:
    """
    Parse the bounding dimensions of an SVG in millimeters.

    KiCad emits SVG user units that are already millimetres (for example
    ``width="30mm" ... viewBox="0 0 30 20"``), which is what the A4 sheet
    builders below rely on when they translate a layer body into page space.

    Returns:
        tuple[float, float, float, float]: ``(min_x, min_y, width_mm, height_mm)``.
        Width/height are ``0.0`` when the file cannot be parsed or carries no
        usable size, so callers must treat a non-positive size as a failure
        rather than silently laying out a wrongly scaled sheet.
    """
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        vb = root.attrib.get("viewBox", "")
        w_str = root.attrib.get("width", "")
        h_str = root.attrib.get("height", "")

        def parse_len(val):
            if not val:
                return None
            val = str(val).strip().lower()
            if val.endswith("mm"):
                return float(val[:-2])
            elif val.endswith("in") or val.endswith("inch"):
                return float(val.replace("inch", "").replace("in", "")) * 25.4
            elif val.endswith("pt"):
                return float(val[:-2]) * (25.4 / 72.0)
            elif val.endswith("cm"):
                return float(val[:-2]) * 10.0
            elif val.endswith("px"):
                return float(val[:-2]) * (25.4 / 96.0)
            try:
                return float(val)
            except ValueError:
                return None

        w_mm = parse_len(w_str)
        h_mm = parse_len(h_str)
        min_x, min_y = 0.0, 0.0

        if vb:
            parts = [float(p) for p in re.split(r"[\s,]+", vb.strip()) if p]
            if len(parts) == 4:
                min_x, min_y = parts[0], parts[1]
                vb_w, vb_h = parts[2], parts[3]
                if w_mm is None:
                    w_mm = vb_w
                if h_mm is None:
                    h_mm = vb_h

        if not w_mm or not h_mm or w_mm <= 0 or h_mm <= 0:
            return (0.0, 0.0, 0.0, 0.0)
        return (min_x, min_y, w_mm, h_mm)
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


def calculate_a4_layout(
    board_w_mm: float,
    board_h_mm: float,
    gap_mm: float = 10.0,
    margin_mm: float = 8.0,
    single_layer: bool = False,
) -> dict:
    """
    Calculate the optimal 2D arrangement for Front and Back copper layers on an A4 sheet.

    Evaluates candidate configurations across both Portrait (210 x 297 mm) and Landscape (297 x 210 mm)
    orientations, testing unrotated (0°) and 90° rotated boards in stacked and side-by-side layouts.
    """
    A4_PORTRAIT = (210.0, 297.0)
    A4_LANDSCAPE = (297.0, 210.0)

    candidates = []

    for page_name, (pw, ph), page_bonus in [("portrait", A4_PORTRAIT, 20.0), ("landscape", A4_LANDSCAPE, 0.0)]:
        usable_w = pw - 2 * margin_mm
        usable_h = ph - 2 * margin_mm

        if single_layer:
            for rotated in [False, True]:
                bw = board_h_mm if rotated else board_w_mm
                bh = board_w_mm if rotated else board_h_mm
                if bw <= usable_w and bh <= usable_h:
                    mx = (pw - bw) / 2.0
                    my = (ph - bh) / 2.0
                    score = page_bonus + (15.0 if not rotated else 0.0) + min(mx, my)
                    candidates.append({
                        "page_orientation": page_name,
                        "page_w": pw, "page_h": ph,
                        "rotated": rotated,
                        "layout_type": "single",
                        "front_pos": (mx, my),
                        "back_pos": None,
                        "board_w": bw, "board_h": bh,
                        "orig_w": board_w_mm, "orig_h": board_h_mm,
                        "gap": 0.0,
                        "score": score,
                    })
        else:
            for rotated in [False, True]:
                bw = board_h_mm if rotated else board_w_mm
                bh = board_w_mm if rotated else board_h_mm

                # 1. Stacked (Vertical: Front on top, Back on bottom)
                min_total_h = 2 * bh + gap_mm
                if bw <= usable_w and min_total_h <= usable_h:
                    extra_h = usable_h - min_total_h
                    actual_gap = min(gap_mm + extra_h * 0.3, 30.0)
                    total_h = 2 * bh + actual_gap
                    mx = (pw - bw) / 2.0
                    my_start = (ph - total_h) / 2.0

                    score = page_bonus + (15.0 if not rotated else 0.0) + 10.0 + min(mx, my_start)
                    candidates.append({
                        "page_orientation": page_name,
                        "page_w": pw, "page_h": ph,
                        "rotated": rotated,
                        "layout_type": "stacked",
                        "front_pos": (mx, my_start),
                        "back_pos": (mx, my_start + bh + actual_gap),
                        "board_w": bw, "board_h": bh,
                        "orig_w": board_w_mm, "orig_h": board_h_mm,
                        "cut_line": (
                            (mx - 4.0, my_start + bh + actual_gap / 2.0),
                            (mx + bw + 4.0, my_start + bh + actual_gap / 2.0),
                        ),
                        "gap": actual_gap,
                        "score": score,
                    })

                # 2. Side-by-Side (Horizontal: Front left, Back right)
                min_total_w = 2 * bw + gap_mm
                if min_total_w <= usable_w and bh <= usable_h:
                    extra_w = usable_w - min_total_w
                    actual_gap = min(gap_mm + extra_w * 0.3, 30.0)
                    total_w = 2 * bw + actual_gap
                    mx_start = (pw - total_w) / 2.0
                    my = (ph - bh) / 2.0

                    score = page_bonus + (15.0 if not rotated else 0.0) + min(mx_start, my)
                    candidates.append({
                        "page_orientation": page_name,
                        "page_w": pw, "page_h": ph,
                        "rotated": rotated,
                        "layout_type": "side_by_side",
                        "front_pos": (mx_start, my),
                        "back_pos": (mx_start + bw + actual_gap, my),
                        "board_w": bw, "board_h": bh,
                        "orig_w": board_w_mm, "orig_h": board_h_mm,
                        "cut_line": (
                            (mx_start + bw + actual_gap / 2.0, my - 4.0),
                            (mx_start + bw + actual_gap / 2.0, my + bh + 4.0),
                        ),
                        "gap": actual_gap,
                        "score": score,
                    })

    if not candidates:
        return {
            "page_orientation": "portrait",
            "page_w": 210.0, "page_h": 297.0,
            "rotated": False,
            "layout_type": "stacked",
            "front_pos": (margin_mm, margin_mm),
            "back_pos": (margin_mm, margin_mm + board_h_mm + 15.0) if not single_layer else None,
            "board_w": board_w_mm, "board_h": board_h_mm,
            "orig_w": board_w_mm, "orig_h": board_h_mm,
            "gap": gap_mm,
            "score": -1,
        }

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[0]


def _build_crosshairs_svg(pos_x: float, pos_y: float, width: float, height: float, offset: float = 2.5) -> str:
    """Generate optical corner fiducials and alignment crosshairs with clear margin from board edges."""
    corners = [
        (pos_x - offset, pos_y - offset),
        (pos_x + width + offset, pos_y - offset),
        (pos_x - offset, pos_y + height + offset),
        (pos_x + width + offset, pos_y + height + offset),
    ]
    elements = []
    for cx, cy in corners:
        elements.append(
            f'<g class="fiducial" stroke="#000000" stroke-width="0.15" fill="none">\n'
            f'  <circle cx="{cx:.3f}" cy="{cy:.3f}" r="1.2" stroke="#000000" stroke-width="0.15" fill="none" />\n'
            f'  <line x1="{cx - 2.5:.3f}" y1="{cy:.3f}" x2="{cx + 2.5:.3f}" y2="{cy:.3f}" stroke="#000000" stroke-width="0.15" />\n'
            f'  <line x1="{cx:.3f}" y1="{cy - 2.5:.3f}" x2="{cx:.3f}" y2="{cy + 2.5:.3f}" stroke="#000000" stroke-width="0.15" />\n'
            f'</g>'
        )
    return "\n".join(elements)


def _build_calibration_ruler_svg(page_w: float, page_h: float, lowest_used_y: float) -> str:
    """
    Build the 50 mm calibration bar used to verify a printer did not rescale the sheet.

    Returns an empty string when the bottom margin cannot hold the bar clear of
    the artwork (it needs ~22 mm below the lowest printed element).
    """
    if (page_h - lowest_used_y) < 22.0:
        return ""
    ticks = "\n".join(
        f'    <line x1="{mm}" y1="-1.2" x2="{mm}" y2="2.2" stroke="#000000" stroke-width="0.2" />'
        for mm in (10, 20, 30, 40)
    )
    return f"""  <!-- 50 mm Scale Bar -->
  <g id="calibration_ruler" transform="translate({(page_w - 50.0) / 2.0:.2f}, {page_h - 12.0:.2f})">
    <rect x="0" y="0" width="50" height="1.0" fill="#000000" />
    <line x1="0" y1="-2.0" x2="0" y2="3.0" stroke="#000000" stroke-width="0.3" />
{ticks}
    <line x1="50" y1="-2.0" x2="50" y2="3.0" stroke="#000000" stroke-width="0.3" />
  </g>"""


def _extract_svg_body(svg_path: str) -> str:
    """Extract graphic elements from an SVG file, preserving exact SVG syntax without namespace prefixes."""
    if not os.path.isfile(svg_path):
        return ""
    try:
        with open(svg_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = re.sub(r"<\?xml[^>]*\?>", "", content)
        content = re.sub(r"<!DOCTYPE[^>]*>", "", content)
        match = re.search(r"<svg\b[^>]*>(.*)</svg>", content, re.DOTALL | re.IGNORECASE)
        if match:
            inner = match.group(1).strip()
            inner = re.sub(r"<title\b[^>]*>.*?</title>", "", inner, flags=re.DOTALL | re.IGNORECASE)
            inner = re.sub(r"<desc\b[^>]*>.*?</desc>", "", inner, flags=re.DOTALL | re.IGNORECASE)
            return inner
        return ""
    except Exception:
        return ""


def generate_a4_merged_svg(
    front_svg_path: str | None,
    back_svg_path: str | None,
    output_svg_path: str,
    pcb_name: str,
    logger: logging.Logger | None = None,
) -> bool:
    """
    Merge front and back copper layer SVGs into an A4 print sheet with coordinate normalization,
    clip-path containment, alignment crosshairs, generous middle spacing, and true 1:1 scale.
    """
    has_front = bool(front_svg_path and os.path.isfile(front_svg_path))
    has_back = bool(back_svg_path and os.path.isfile(back_svg_path))

    if not has_front and not has_back:
        return False

    ref_svg = front_svg_path if has_front else back_svg_path
    _, _, board_w, board_h = parse_svg_dimensions(ref_svg)
    if board_w <= 0 or board_h <= 0:
        if logger:
            logger.warning("Could not determine board size from '%s'; skipping A4 homebrew sheet.", ref_svg)
        return False

    single_layer = not (has_front and has_back)
    layout = calculate_a4_layout(board_w, board_h, gap_mm=15.0, margin_mm=8.0, single_layer=single_layer)

    if layout.get("score", 0) < 0:
        if logger:
            logger.warning("Board dimensions (%.1f x %.1f mm) exceed printable single-page A4 area.", board_w, board_h)
        return False

    pw, ph = layout["page_w"], layout["page_h"]
    rotated = layout["rotated"]
    bw, bh = layout["board_w"], layout["board_h"]
    orig_w, orig_h = layout["orig_w"], layout["orig_h"]

    clip_defs = []
    layers_svg = []

    # Front Board
    if has_front and layout["front_pos"]:
        fx, fy = layout["front_pos"]
        min_fx, min_fy, _, _ = parse_svg_dimensions(front_svg_path)
        body_f = _extract_svg_body(front_svg_path)

        clip_defs.append(
            f'    <clipPath id="clip_front_copper">\n'
            f'      <rect x="{fx:.3f}" y="{fy:.3f}" width="{bw:.3f}" height="{bh:.3f}" />\n'
            f'    </clipPath>'
        )

        if rotated:
            transform_f = f'translate({fx + orig_h:.3f}, {fy:.3f}) rotate(90) translate({-min_fx:.3f}, {-min_fy:.3f})'
        else:
            transform_f = f'translate({fx - min_fx:.3f}, {fy - min_fy:.3f})'

        content_f = (
            f'  <g id="layer_front_copper" clip-path="url(#clip_front_copper)">\n'
            f'    <g transform="{transform_f}">\n'
            f'{body_f}\n'
            f'    </g>\n'
            f'  </g>'
        )
        crosshairs_f = _build_crosshairs_svg(fx, fy, bw, bh)
        mark_f = f'  <text x="{fx + bw / 2.0:.2f}" y="{fy - 2.5:.2f}" font-family="-apple-system, BlinkMacSystemFont, Arial, sans-serif" font-size="2.2" font-weight="bold" fill="#000000" text-anchor="middle" class="layer-mark">F.Cu</text>'
        layers_svg.append(f"  <!-- FRONT COPPER LAYER (F.Cu) -->\n{content_f}\n{crosshairs_f}\n{mark_f}")

    # Back Board
    if has_back and layout["back_pos"]:
        bx, by = layout["back_pos"]
        min_bx, min_by, _, _ = parse_svg_dimensions(back_svg_path)
        body_b = _extract_svg_body(back_svg_path)

        clip_defs.append(
            f'    <clipPath id="clip_back_copper">\n'
            f'      <rect x="{bx:.3f}" y="{by:.3f}" width="{bw:.3f}" height="{bh:.3f}" />\n'
            f'    </clipPath>'
        )

        if rotated:
            transform_b = f'translate({bx + orig_h:.3f}, {by:.3f}) rotate(90) translate({-min_bx:.3f}, {-min_by:.3f})'
        else:
            transform_b = f'translate({bx - min_bx:.3f}, {by - min_by:.3f})'

        content_b = (
            f'  <g id="layer_back_copper" clip-path="url(#clip_back_copper)">\n'
            f'    <g transform="{transform_b}">\n'
            f'{body_b}\n'
            f'    </g>\n'
            f'  </g>'
        )
        crosshairs_b = _build_crosshairs_svg(bx, by, bw, bh)
        mark_b = f'  <text x="{bx + bw / 2.0:.2f}" y="{by - 2.5:.2f}" font-family="-apple-system, BlinkMacSystemFont, Arial, sans-serif" font-size="2.2" font-weight="bold" fill="#000000" text-anchor="middle" class="layer-mark">B.Cu</text>'
        layers_svg.append(f"  <!-- BACK COPPER LAYER (B.Cu) -->\n{content_b}\n{crosshairs_b}\n{mark_b}")

    # Cut/fold separator line
    cut_line_svg = ""
    if "cut_line" in layout and has_front and has_back:
        (x1, y1), (x2, y2) = layout["cut_line"]
        cut_line_svg = f'  <line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="#000000" stroke-width="0.2" stroke-dasharray="2, 2" class="guide" id="cut_guide" />'

    # Calibration scale bar (skipped when the bottom margin is too tight)
    lowest_y = max(
        (layout["front_pos"][1] + bh) if layout.get("front_pos") else 0,
        (layout["back_pos"][1] + bh) if layout.get("back_pos") else 0,
    )
    calibration_svg = _build_calibration_ruler_svg(pw, ph, lowest_y)

    all_defs_str = "\n".join(clip_defs)
    all_layers_str = "\n\n".join(layers_svg)

    svg_doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1"
     width="{pw:.1f}mm" height="{ph:.1f}mm" viewBox="0 0 {pw:.1f} {ph:.1f}">
  <style>
    .layer-mark {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; font-size: 2.2mm; font-weight: bold; fill: #000000; text-anchor: middle; }}
    .guide {{ stroke: #000000; stroke-width: 0.2; stroke-dasharray: 2, 2; }}
    .fiducial {{ stroke: #000000; stroke-width: 0.15; fill: none; }}
    .ruler-bar {{ fill: #000000; }}
  </style>

  <defs>
{all_defs_str}
  </defs>

  <!-- Solid White A4 Sheet Background -->
  <rect width="{pw:.1f}" height="{ph:.1f}" fill="#FFFFFF" />

{all_layers_str}

{cut_line_svg}

{calibration_svg}
</svg>
"""

    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_svg_path)), exist_ok=True)
        with open(output_svg_path, "w", encoding="utf-8") as f:
            f.write(svg_doc)
        if logger:
            logger.info("Generated merged A4 homebrew SVG: %s", output_svg_path)
        return True
    except Exception as exc:
        if logger:
            logger.error("Failed to write merged A4 SVG '%s': %s", output_svg_path, exc)
        return False


def fit_board_on_a4_page(
    board_w_mm: float,
    board_h_mm: float,
    margin_mm: float = 8.0,
) -> dict | None:
    """
    Find a 1:1 placement for one board on a single A4 page.

    Portrait is preferred, then portrait rotated 90 deg, then landscape, then
    landscape rotated. Returns ``None`` when the board does not fit any of them
    at true scale — callers must not fall back to cropping, because a scaled or
    clipped etching sheet is worse than no sheet at all.
    """
    if board_w_mm <= 0 or board_h_mm <= 0:
        return None
    for page_orientation, (pw, ph) in (("portrait", (210.0, 297.0)), ("landscape", (297.0, 210.0))):
        for rotated in (False, True):
            w = board_h_mm if rotated else board_w_mm
            h = board_w_mm if rotated else board_h_mm
            if w <= pw - 2 * margin_mm and h <= ph - 2 * margin_mm:
                return {
                    "page_orientation": page_orientation,
                    "page_w": pw,
                    "page_h": ph,
                    "rotated": rotated,
                    "board_w": w,
                    "board_h": h,
                    "orig_w": board_w_mm,
                    "orig_h": board_h_mm,
                    "pos": ((pw - w) / 2.0, (ph - h) / 2.0),
                }
    return None


def generate_single_a4_sheet_svg(
    layer_svg_path: str,
    output_svg_path: str,
    mark: str = "F.Cu",
    logger: logging.Logger | None = None,
) -> bool:
    """Generate a single-layer A4 sheet for 1-board-per-page multi-page homebrew PDF export."""
    if not os.path.isfile(layer_svg_path):
        return False
    try:
        min_x, min_y, bw, bh = parse_svg_dimensions(layer_svg_path)
        body = _extract_svg_body(layer_svg_path)

        fit = fit_board_on_a4_page(bw, bh)
        if fit is None:
            if logger:
                logger.warning(
                    "Board (%.1f x %.1f mm) does not fit a single A4 page at 1:1; "
                    "skipping homebrew sheet for %s rather than cropping it.",
                    bw, bh, mark,
                )
            return False

        pw, ph = fit["page_w"], fit["page_h"]
        rotated = fit["rotated"]
        orig_w, orig_h = fit["orig_w"], fit["orig_h"]
        w, h = fit["board_w"], fit["board_h"]
        x, y = fit["pos"]

        if rotated:
            transform = f'translate({x + orig_h:.3f}, {y:.3f}) rotate(90) translate({-min_x:.3f}, {-min_y:.3f})'
        else:
            transform = f'translate({x - min_x:.3f}, {y - min_y:.3f})'

        crosshairs = _build_crosshairs_svg(x, y, w, h)
        layer_id = "layer_front_copper" if "F" in mark else "layer_back_copper"
        clip_id = "clip_single_front" if "F" in mark else "clip_single_back"

        svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1"
     width="{pw:.1f}mm" height="{ph:.1f}mm" viewBox="0 0 {pw:.1f} {ph:.1f}">
  <style>
    .layer-mark {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; font-size: 2.5mm; font-weight: bold; fill: #000000; text-anchor: middle; }}
    .fiducial {{ stroke: #000000; stroke-width: 0.15; fill: none; }}
    .ruler-bar {{ fill: #000000; }}
  </style>
  <defs>
    <clipPath id="{clip_id}">
      <rect x="{x:.3f}" y="{y:.3f}" width="{w:.3f}" height="{h:.3f}" />
    </clipPath>
  </defs>
  <rect width="{pw:.1f}" height="{ph:.1f}" fill="#FFFFFF" />
  <g id="{layer_id}" clip-path="url(#{clip_id})">
    <g transform="{transform}">
{body}
    </g>
  </g>
{crosshairs}
  <text x="{x + w / 2.0:.2f}" y="{y - 3.0:.2f}" font-family="-apple-system, BlinkMacSystemFont, Arial, sans-serif" font-size="2.5" font-weight="bold" fill="#000000" text-anchor="middle" class="layer-mark">{mark}</text>
{_build_calibration_ruler_svg(pw, ph, y + h)}
</svg>"""
        os.makedirs(os.path.dirname(os.path.abspath(output_svg_path)), exist_ok=True)
        with open(output_svg_path, "w", encoding="utf-8") as f:
            f.write(svg)
        return True
    except Exception as exc:
        if logger:
            logger.error("Failed to write single A4 sheet '%s': %s", output_svg_path, exc)
        return False


PRINT_PDF_DPI = 1200
# Raster fallback resolutions, tried in order. A full A4 page at 1200 DPI is
# ~139 megapixels and needs well over a gigabyte while it is copied through
# wx.Bitmap -> wx.Image -> bytes -> PIL, so drop to 600 DPI rather than fail.
PRINT_PDF_RASTER_DPI_LADDER = (1200, 600)
# External converters tried last, as (executable, argv builder, supports_multi_page).
# rsvg-convert accepts multiple input files and emits one page per file when the
# output is PDF, so it is the only converter usable for the oversized-board
# multi-page fallback; Inkscape's CLI only ever produces one page per invocation.
# This is also the only PDF tier that actually works inside the shipped Docker
# Action image (no PyQt6, no wx.App there) -- see the Dockerfile's librsvg2-bin.
PRINT_PDF_CLI_CONVERTERS = (
    ("inkscape", lambda exe, svgs, pdf: [exe, svgs[0], f"--export-filename={pdf}", f"--export-dpi={PRINT_PDF_DPI}"], False),
    ("rsvg-convert", lambda exe, svgs, pdf: [exe, "-f", "pdf", "-d", str(PRINT_PDF_DPI), "-p", str(PRINT_PDF_DPI), "-o", pdf, *svgs], True),
)
PRINT_PDF_CLI_TIMEOUT_SEC = 180


def _is_headless_session() -> bool:
    """True when no windowing system is reachable (CI, Docker, plain ssh session)."""
    if sys.platform in ("win32", "darwin"):
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _discard_file(path: str) -> None:
    """Remove a partially written output so a failed tier cannot masquerade as success."""
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# Holds the sole QGuiApplication this process ever creates. PyQt6 owns the
# wrapped C++ object by refcount: a QGuiApplication built without keeping a
# reference is garbage-collected almost immediately, which deletes the C++
# singleton but leaves QGuiApplication::instance() dangling - later Qt calls
# (QPainter, QSvgRenderer) then read freed memory and crash the whole
# interpreter with no Python exception to catch. This module-level slot is
# what keeps that reference alive for the process lifetime.
_qt_app_ref = None


def _export_pdf_via_qt(svg_paths: list[str], output_pdf_path: str, is_landscape: bool, logger) -> bool:
    """Tier 1 - true vector PDF through PyQt6's QPdfWriter."""
    global _qt_app_ref
    # Qt calls qFatal() (which abort()s the process, uncatchable from Python)
    # when it cannot open a display, so force the offscreen platform plugin
    # before QGuiApplication is constructed.
    if _is_headless_session():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6 import QtCore, QtGui, QtSvg

    app = QtGui.QGuiApplication.instance()
    if app is None:
        app = QtGui.QGuiApplication([])
    _qt_app_ref = app  # see module comment above - must outlive this call
    writer = QtGui.QPdfWriter(output_pdf_path)
    writer.setPageSize(QtGui.QPageSize(QtGui.QPageSize.PageSizeId.A4))
    writer.setPageOrientation(
        QtGui.QPageLayout.Orientation.Landscape if is_landscape
        else QtGui.QPageLayout.Orientation.Portrait
    )
    writer.setResolution(PRINT_PDF_DPI)
    writer.setPageMargins(QtCore.QMarginsF(0, 0, 0, 0))

    painter = QtGui.QPainter(writer)
    try:
        for idx, sp in enumerate(svg_paths):
            renderer = QtSvg.QSvgRenderer(sp)
            # An unparsable SVG makes render() a silent no-op, which would
            # otherwise ship a blank-but-non-empty PDF as a success.
            if not renderer.isValid():
                raise ValueError(f"Qt could not parse SVG: {sp}")
            if idx > 0:
                writer.newPage()
            renderer.render(painter)
    finally:
        painter.end()

    if os.path.isfile(output_pdf_path) and os.path.getsize(output_pdf_path) > 0:
        if logger:
            logger.info(
                "Exported %d DPI vector PDF (%d page(s)) via PyQt6: %s",
                PRINT_PDF_DPI, len(svg_paths), output_pdf_path,
            )
        return True
    return False


def _export_pdf_via_wx(svg_paths: list[str], output_pdf_path: str, is_landscape: bool, logger) -> bool:
    """Tier 2 - high-resolution 1-bit raster PDF through wxPython's SVG rasterizer."""
    import wx
    from PIL import Image

    # wx.App() raises SystemExit (not Exception) when no display is available,
    # which would tear the whole export process down. Never construct one here;
    # reuse the running Studio app or defer to the next tier.
    if wx.GetApp() is None:
        raise RuntimeError("no wx.App available for SVG rasterization")

    page_mm = (297.0, 210.0) if is_landscape else (210.0, 297.0)
    last_error = None
    for dpi in PRINT_PDF_RASTER_DPI_LADDER:
        size = wx.Size(int(page_mm[0] / 25.4 * dpi), int(page_mm[1] / 25.4 * dpi))
        frames = []
        try:
            for sp in svg_paths:
                bundle = wx.BitmapBundle.FromSVGFile(sp, size)
                if not bundle.IsOk():
                    raise ValueError(f"wx could not rasterize SVG: {sp}")
                img = bundle.GetBitmap(size).ConvertToImage()
                pil_img = Image.frombuffer(
                    "RGB", (img.GetWidth(), img.GetHeight()), img.GetData(), "raw", "RGB", 0, 1
                )
                frames.append(pil_img.convert("1", dither=Image.Dither.NONE))
                del pil_img, img
            frames[0].save(
                output_pdf_path, "PDF", resolution=float(dpi),
                save_all=True, append_images=frames[1:],
            )
        except (MemoryError, ValueError, OSError) as exc:
            last_error = exc
            _discard_file(output_pdf_path)
            if logger:
                logger.debug("wx+Pillow PDF export at %d DPI failed: %s", dpi, exc)
            continue
        finally:
            frames.clear()

        if os.path.isfile(output_pdf_path) and os.path.getsize(output_pdf_path) > 0:
            if logger:
                logger.info(
                    "Exported %d DPI PDF (%d page(s)) via wx+Pillow: %s",
                    dpi, len(svg_paths), output_pdf_path,
                )
            return True
    if last_error is not None:
        raise last_error
    return False


# Worker run by _export_pdf_via_subprocess. It imports this very module in a
# fresh interpreter and calls the in-process Qt tier from that process's own
# main thread -- the only thread Qt allows a QGuiApplication to be built on --
# so the rendering code has exactly one implementation rather than a duplicate
# maintained for out-of-process use.
# Holds the worker process's wx.App. wx tracks the "current" app weakly, so an
# App that nothing references is collected straight after construction and
# wx.GetApp() goes back to None -- keep it alive for the process's lifetime.
_pdf_worker_wx_app = None


def _pdf_worker_main(fn_name: str, svg_paths: list[str], output_pdf_path: str,
                     is_landscape: bool) -> bool:
    """
    Entry point executed inside a rendering worker process.

    Establishes whatever application object the chosen tier needs before
    handing off to it. :func:`_export_pdf_via_wx` deliberately refuses to
    construct a ``wx.App`` itself -- in-process that would either collide with
    Studio's own app or, on a headless host, raise SystemExit and tear the
    whole export down. Here the process exists solely to render, this is its
    main thread, and a failure kills only the worker, which the parent simply
    reports as that tier being unavailable.
    """
    if fn_name == "_export_pdf_via_wx":
        import wx
        if wx.GetApp() is None:
            global _pdf_worker_wx_app
            _pdf_worker_wx_app = wx.App(False)
    return bool(globals()[fn_name](svg_paths, output_pdf_path, is_landscape, None))


_PDF_WORKER_SRC = (
    "import json, sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "import kiforge;"
    "a = json.loads(sys.argv[2]);"
    "sys.exit(0 if kiforge._pdf_worker_main("
    "a['fn'], a['svgs'], a['out'], a['landscape']) else 1)"
)

# How often the parent looks at should_abort() while the worker renders. Small
# enough that Cancel feels immediate, large enough not to spin a core.
_QT_PDF_POLL_SEC = 0.2


def _export_pdf_via_subprocess(
    tier_func: str,
    svg_paths: list[str],
    output_pdf_path: str,
    is_landscape: bool,
    logger,
    python_exe: str | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    """
    Run one of the GUI-toolkit PDF tiers out-of-process.

    ``tier_func`` names the in-process tier to run (``_export_pdf_via_qt`` or
    ``_export_pdf_via_wx``); the worker imports this module and calls it, so
    each renderer keeps exactly one implementation.

    Both toolkits insist on building their application object on the
    process's main thread. In-process that means hijacking the *GUI* thread:
    the UI freezes for the whole render and a render already under way cannot
    be interrupted. A separate interpreter has its own main thread, so nothing
    is marshalled onto the GUI thread at all, and the render is a plain child
    process -- which means it can simply be killed when the user cancels.
    """
    exe = python_exe or sys.executable
    if not exe or not os.path.isfile(exe):
        return False
    payload = json.dumps({
        "fn": tier_func,
        "svgs": list(svg_paths),
        "out": output_pdf_path,
        "landscape": bool(is_landscape),
    })
    module_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        proc = subprocess.Popen(
            [exe, "-c", _PDF_WORKER_SRC, module_dir, payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_subprocess_startupinfo(),
        )
    except (OSError, ValueError) as exc:
        if logger:
            logger.debug("%s subprocess PDF export could not start: %s", tier_func, exc)
        return False

    try:
        polls_left = int(PRINT_PDF_CLI_TIMEOUT_SEC / _QT_PDF_POLL_SEC)
        while True:
            try:
                proc.wait(timeout=_QT_PDF_POLL_SEC)
                break
            except subprocess.TimeoutExpired:
                if should_abort is not None and should_abort():
                    _terminate_subprocess(proc)
                    _discard_file(output_pdf_path)
                    if logger:
                        logger.info("%s subprocess PDF export cancelled.", tier_func)
                    return False
                polls_left -= 1
                if polls_left <= 0:
                    _terminate_subprocess(proc)
                    _discard_file(output_pdf_path)
                    if logger:
                        logger.debug("%s subprocess PDF export timed out.", tier_func)
                    return False

        if proc.returncode == 0 and os.path.isfile(output_pdf_path) and os.path.getsize(output_pdf_path) > 0:
            if logger:
                logger.info("Exported PDF via %s subprocess: %s", tier_func, output_pdf_path)
            return True
        if logger:
            err = ""
            if proc.stderr is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            logger.debug(
                "%s subprocess PDF export unavailable or failed: %s" % tier_func,
                err.splitlines()[-1] if err else proc.returncode,
            )
        _discard_file(output_pdf_path)
        return False
    finally:
        # Popen(stdout=PIPE, stderr=PIPE) opens two pipes that nothing else
        # closes on these paths -- without this each render leaks two file
        # descriptors, and an export renders several PDFs.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _export_pdf_via_cli(
    svg_paths: list[str],
    output_pdf_path: str,
    logger,
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    """
    Tier 3 - external converters (Inkscape: single page only; rsvg-convert: any page count).

    ``should_abort`` is checked before each converter for the same reason the
    tier ladder checks it: a cancelled export must not start the next
    converter and sit through another full conversion.
    """
    for name, build_argv, supports_multi in PRINT_PDF_CLI_CONVERTERS:
        if should_abort is not None and should_abort():
            _discard_file(output_pdf_path)
            return False
        if len(svg_paths) != 1 and not supports_multi:
            continue
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            res = subprocess.run(
                build_argv(exe, svg_paths, output_pdf_path),
                capture_output=True,
                timeout=PRINT_PDF_CLI_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if logger:
                logger.debug("%s PDF export failed: %s", name, exc)
            _discard_file(output_pdf_path)
            continue
        if res.returncode == 0 and os.path.isfile(output_pdf_path) and os.path.getsize(output_pdf_path) > 0:
            if logger:
                logger.info("Exported PDF via %s: %s", name, output_pdf_path)
            return True
        _discard_file(output_pdf_path)
    return False


def _gui_thread_tier_would_block() -> bool:
    """
    True when running a GUI-thread tier would freeze a live UI.

    Marshalling work onto the GUI thread (see :func:`_run_on_gui_thread`) runs
    it *inside* that thread's event loop, so for however long the render takes
    the loop dispatches nothing else: timers stop firing, windows stop
    repainting and clicks queue up unhandled -- which is what made Studio's
    progress dialog freeze mid-export and the OS mark it "Not Responding".
    Only true when a GUI loop actually exists and we are not already on it;
    the CLI and CD/Action entry points have no loop to block.
    """
    if threading.current_thread() is threading.main_thread():
        return False
    try:
        import wx
        return wx.GetApp() is not None
    except Exception:
        return False


def _run_on_gui_thread(fn, timeout: float = 180.0):
    """
    Run ``fn()`` on the thread that owns the process's GUI event loop, and
    return its result.

    Qt and wxPython may only construct or drive their application/window
    objects from the thread that owns the platform's native event loop -
    Cocoa enforces this strictly on macOS, so calling ``QGuiApplication([])``
    or wx's SVG rasterizer from a background thread can abort the whole host
    process, not just this export. KiForge Studio always runs exports on a
    background worker thread (to keep the UI responsive), so every call into
    :func:`export_svg_to_1200dpi_pdf` from there needs marshaling; the CLI and
    CD/Action entry points call it on the main thread already, so this is a
    no-op there.
    """
    if threading.current_thread() is threading.main_thread():
        return fn()
    try:
        import wx
        app = wx.GetApp()
    except Exception:
        app = None
    if app is None:
        # No GUI event loop is running (headless CLI/CD) - safe to call directly.
        return fn()

    done = threading.Event()
    abandoned = threading.Event()
    outcome = {}

    def _invoke():
        # If the wait below already timed out, the caller has moved on to the
        # next tier (or given up) and may already be reading/deleting
        # output_pdf_path. Running fn() now would write to that same path
        # behind the caller's back, silently clobbering whatever a later tier
        # produced - so skip it entirely once abandoned.
        if abandoned.is_set():
            return
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            outcome["error"] = exc
        finally:
            done.set()

    wx.CallAfter(_invoke)
    # Bounded, not indefinite: if the GUI thread's event loop never picks the
    # callback up (dialog destroyed, app quitting), the worker must not hang
    # forever -- it falls through and the caller tries the next tier instead.
    if not done.wait(timeout=timeout):
        abandoned.set()
        raise TimeoutError(f"GUI thread did not become available to render the PDF within {timeout:.0f}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result", False)


def export_svg_to_1200dpi_pdf(
    svg_path: str | list[str],
    output_pdf_path: str,
    is_landscape: bool = False,
    logger: logging.Logger | None = None,
    should_abort: Callable[[], bool] | None = None,
    python_exe: str | None = None,
) -> bool:
    """
    Render SVG file(s) into a true 1:1 scale 1200 DPI vector / high-res PDF.

    Accepts either a single SVG file path (1-page PDF) or a list of SVG paths
    (multi-page PDF). Tiers are tried in order - PyQt6 vector, wxPython raster,
    then external CLI converters - and each tier removes its own partial output
    so a later tier (or the caller) never sees a half-written PDF.

    ``is_landscape`` must describe the sheets being rendered: they are drawn 1:1
    into the page box, so a mismatch silently rescales the whole artwork.

    ``should_abort`` is consulted between tiers (pass ``context.is_aborted``)
    so a cancelled export stops here instead of working through every
    remaining tier first -- each of which can occupy the GUI thread for up to
    the :func:`_run_on_gui_thread` timeout before the pipeline gets its next
    chance to notice the cancellation.
    """
    svg_paths = [svg_path] if isinstance(svg_path, str) else list(svg_path)
    svg_paths = [p for p in svg_paths if p and os.path.isfile(p)]
    if not svg_paths:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)), exist_ok=True)

    # The GUI-toolkit tiers construct/drive real Qt or wx application objects
    # and must run on the GUI thread (see _run_on_gui_thread); the subprocess
    # tier has no such constraint and stays on the calling thread so it never
    # blocks Studio's UI while an external converter runs.
    def _sub(fn_name):
        return lambda: _export_pdf_via_subprocess(
            fn_name, svg_paths, output_pdf_path, is_landscape, logger, python_exe, should_abort)

    # Ordered by what is actually guaranteed to be there, not by what is
    # nicest when it happens to be installed:
    #
    #   wx   KiCad ships wxPython for its own GUI on Windows, Linux and macOS
    #        alike, so this tier is available wherever the plugin can run at
    #        all. It rasterizes, which is the right trade here: the output is
    #        a 1200 DPI sheet meant to be printed for etching, and every
    #        machine producing byte-comparable artwork matters more for a
    #        manufacturing file than an occasional vector upgrade that only
    #        some installs would get.
    #   CLI  rsvg-convert / Inkscape are common on Linux, absent as often as
    #        not elsewhere -- an upgrade when present, never depended on.
    #   Qt   PyQt6 is not part of KiCad on any platform; it only exists when
    #        running under a system Python that happens to have it (CLI/CD).
    #
    # Every GUI-toolkit renderer is offered out-of-process first; the
    # in-process variants stay as a last resort for hosts where spawning the
    # worker is impossible (no usable interpreter, restricted environment),
    # and are the only tiers that can block the GUI thread.
    tiers = (
        ("wx+Pillow (subprocess)", _sub("_export_pdf_via_wx"), False),
        ("PyQt6 (subprocess)", _sub("_export_pdf_via_qt"), False),
        ("wx+Pillow", lambda: _export_pdf_via_wx(svg_paths, output_pdf_path, is_landscape, logger), True),
        ("PyQt6", lambda: _export_pdf_via_qt(svg_paths, output_pdf_path, is_landscape, logger), True),
        ("CLI converter",
         lambda: _export_pdf_via_cli(svg_paths, output_pdf_path, logger, should_abort), False),
    )
    # Quality order above (vector Qt first) is the right default, but it is
    # only a preference: when a GUI-thread tier would freeze a live UI, the
    # subprocess tier -- which renders just as well and needs no GUI thread --
    # is tried first instead, so Studio keeps animating and stays clickable.
    # Sorting on the tier's own "needs the GUI thread" flag keeps this a
    # property of the tiers rather than a second hand-maintained order.
    if _gui_thread_tier_would_block():
        tiers = tuple(sorted(tiers, key=lambda t: t[2]))

    for tier_name, tier, on_gui_thread in tiers:
        if should_abort is not None and should_abort():
            if logger:
                logger.info("PDF export cancelled before the %s tier.", tier_name)
            _discard_file(output_pdf_path)
            return False
        try:
            ok = _run_on_gui_thread(tier) if on_gui_thread else tier()
            if ok:
                return True
        # SystemExit is deliberate: wxPython raises it (a BaseException) on a
        # headless host, and it must not terminate the whole export run.
        except (Exception, SystemExit) as exc:
            if logger:
                logger.debug("%s PDF export unavailable or failed: %s", tier_name, exc)
        _discard_file(output_pdf_path)

    return False


# kicad-cli flags shared by the copper-layer SVG exports. ``--page-size-mode 2``
# crops the plot to the board bounding box, which is what makes 1:1 A4 placement
# possible; older kicad-cli builds reject it, hence the fallback without it.
_SVG_LAYER_SPECS = {
    "front": ("F.Cu,Edge.Cuts", ()),
    "back": ("B.Cu,Edge.Cuts", ("-m",)),
}


def _copper_svg_command(context: "ExportContext", side: str, output_path: str, board_area_only: bool) -> list[str]:
    """Build the kicad-cli argv that plots one copper layer as a black & white SVG."""
    layers, extra_flags = _SVG_LAYER_SPECS[side]
    cmd = [
        context.kicad_cli, "pcb", "export", "svg",
        "-l", layers, *extra_flags, "-n", "--drill-shape-opt", "2",
        "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
    ]
    if board_area_only:
        cmd += ["--page-size-mode", "2", "--mode-single"]
    cmd += ["--output", output_path, "--black-and-white", context.pcb_file]
    return cmd


def _export_copper_layer(task: "ExportTask", context: ExportContext, side: str, output_path: str) -> tuple[bool, bool]:
    """
    Plot one copper layer, retrying without --page-size-mode for older kicad-cli builds.

    A free function (not a method) so both :class:`SvgExportTask` and
    :class:`HomebrewPdfExportTask` can call it as themselves via ``task`` --
    subprocess failures then log under the caller's own task name instead of a
    borrowed one.

    Returns ``(ok, board_area_cropped)``. ``board_area_cropped`` is False on
    the fallback path: that SVG's page is the project's full drawing sheet,
    not the board's bounding box, so its dimensions must never be fed into
    the A4 homebrew layout math (which needs the true board size).
    """
    if task._run_subprocess(_copper_svg_command(context, side, output_path, True), context):
        return True, True
    if context.is_aborted():
        return False, False
    ok = task._run_subprocess(_copper_svg_command(context, side, output_path, False), context)
    return ok, False


def export_copper_layers(task: "ExportTask", context: ExportContext, target_dir: str) -> tuple[str | None, str | None, bool]:
    """
    Plot both copper layers into ``target_dir`` on behalf of ``task``.

    Returns ``(front_path, back_path, board_area_cropped)``: paths are
    ``None`` for any layer that did not produce a file, so callers never
    merge a half-written SVG; ``board_area_cropped`` is True only when every
    produced layer used the board-area-cropped export (see
    :func:`_export_copper_layer`).
    """
    paths: dict[str, str | None] = {}
    cropped_all = True
    for side in ("front", "back"):
        if context.is_aborted():
            # Same rule as the retry inside _export_copper_layer: a cancelled
            # run must not start plotting the next layer.
            break
        out = os.path.join(target_dir, f"{context.pcb_name}_{side}.svg")
        ok, cropped = _export_copper_layer(task, context, side, out)
        paths[side] = out if (ok and os.path.isfile(out)) else None
        if paths[side] is not None and not cropped:
            cropped_all = False
    if paths["front"] is None and paths["back"] is not None:
        context.logger.warning("Front SVG export failed; back layer was exported anyway.")
    elif paths["back"] is None and paths["front"] is not None:
        context.logger.warning("Back SVG export failed; front layer was exported anyway.")
    return paths["front"], paths["back"], cropped_all


class SvgExportTask(ExportTask):
    """Export front/back copper SVGs and merged A4 homebrew sheet (``{pcb_name}_front.svg``, ``{pcb_name}_back.svg``, ``{pcb_name}_homebrew.svg``)."""

    def __init__(self):
        super().__init__("Exporting Copper SVGs")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_svg", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        front_path, back_path, cropped = export_copper_layers(self, context, context.output_dir)
        if not front_path and not back_path:
            return False

        homebrew_svg = None
        if cropped:
            homebrew_svg = os.path.join(context.output_dir, f"{context.pcb_name}_homebrew.svg")
            if not generate_a4_merged_svg(
                front_path, back_path, homebrew_svg, context.pcb_name, logger=context.logger
            ):
                homebrew_svg = None
                context.logger.info(
                    "Single-page A4 homebrew SVG skipped (board does not fit A4 at 1:1); layer SVGs preserved."
                )
        else:
            context.logger.info(
                "Copper SVGs plotted without board-area cropping (older kicad-cli build); "
                "homebrew A4 sheet skipped since the layer size can't be trusted for 1:1 placement."
            )

        # Let HomebrewPdfExportTask reuse this work instead of re-plotting and
        # re-merging the same sheet from scratch.
        context.homebrew_layers = {
            "front": front_path,
            "back": back_path,
            "cropped": cropped,
            "homebrew_svg": homebrew_svg,
        }
        return True


class HomebrewPdfExportTask(ExportTask):
    """Export 1200 DPI homebrew etching & mask PDF (``{pcb_name}_homebrew.pdf``)."""

    def __init__(self):
        super().__init__("Exporting Homebrew PDF")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_print_pdf", True) and bool(context.pcb_file)

    def _resolve_layers(self, context: ExportContext, temp_dir: str) -> tuple[str | None, str | None, bool]:
        """
        Get the copper-layer SVGs and whether they are safe for 1:1 A4 placement.

        Reuses :class:`SvgExportTask`'s work (stashed on ``context.homebrew_layers``)
        when it already ran this pipeline pass, retrying only a layer that is
        still missing rather than accepting a partial result as final. Falls
        back to plotting both layers from scratch when ``export_svg`` is
        disabled (nothing lands in ``context.output_dir`` in that case).

        Layer plotting is always run via ``self`` (this task's own
        ``_run_subprocess``), so a failure is attributed to "Exporting Homebrew
        PDF" and not misreported under a borrowed "Exporting Copper SVGs" name.
        """
        cached = context.homebrew_layers
        if cached and (cached["front"] or cached["back"]):
            front, back, cropped = cached["front"], cached["back"], cached["cropped"]
            for side, existing in (("front", front), ("back", back)):
                if existing or context.is_aborted():
                    continue
                # SvgExportTask produced only one side; retry the missing one
                # rather than silently shipping a single-sided homebrew PDF.
                out = os.path.join(temp_dir, f"{context.pcb_name}_{side}.svg")
                ok, side_cropped = _export_copper_layer(self, context, side, out)
                if ok and os.path.isfile(out):
                    if side == "front":
                        front = out
                    else:
                        back = out
                    cropped = cropped and side_cropped
            return front, back, cropped
        return export_copper_layers(self, context, temp_dir)

    def run(self, context: ExportContext) -> bool:
        homebrew_pdf = os.path.join(context.output_dir, f"{context.pcb_name}_homebrew.pdf")
        # The merged sheet only belongs in the output folder when the user asked
        # for SVGs; otherwise it is a scratch intermediate for the PDF.
        keep_svg = bool(context.options.get("export_svg", True))
        temp_dir = tempfile.mkdtemp(prefix="kiforge_homebrew_")
        try:
            context.report_progress(0.05)
            front_path, back_path, cropped = self._resolve_layers(context, temp_dir)
            if not front_path and not back_path:
                context.add_warning("No copper layer SVG available; homebrew PDF skipped.")
                return False
            if not cropped:
                context.add_warning(
                    "Homebrew PDF skipped: copper layers were plotted without board-area "
                    "cropping (older kicad-cli build), so their size can't be trusted for 1:1 A4 placement."
                )
                return False

            # Reuse the merged sheet SvgExportTask already wrote this pass
            # instead of re-parsing and re-merging the same layers again.
            cached_svg = context.homebrew_layers.get("homebrew_svg") if context.homebrew_layers else None
            merged_ok = bool(cached_svg and os.path.isfile(cached_svg))
            homebrew_svg = cached_svg if merged_ok else os.path.join(
                context.output_dir if keep_svg else temp_dir,
                f"{context.pcb_name}_homebrew.svg",
            )
            if not merged_ok:
                merged_ok = generate_a4_merged_svg(
                    front_path, back_path, homebrew_svg, context.pcb_name, logger=context.logger
                )

            context.report_progress(0.35)
            if merged_ok:
                # The sheet is drawn 1:1, so the PDF page must match the sheet's
                # own orientation - deriving it any other way rescales the plot.
                _, _, sheet_w, sheet_h = parse_svg_dimensions(homebrew_svg)
                context.report_progress(0.5)
                pdf_ok = export_svg_to_1200dpi_pdf(
                    homebrew_svg, homebrew_pdf,
                    is_landscape=sheet_w > sheet_h, logger=context.logger,
                    should_abort=context.is_aborted, python_exe=context.kicad_python,
                )
            else:
                # Both layers do not share one A4 sheet: fall back to one board
                # per page (page 1 front, page 2 back), still at true scale.
                context.logger.info("Board cannot share a single A4 page; generating multi-page homebrew PDF.")
                page_svgs = []
                sheets = ((front_path, "F.Cu", "page1_front"), (back_path, "B.Cu", "page2_back"))
                for sheet_no, (path, mark, page) in enumerate(sheets, start=1):
                    if context.is_aborted():
                        return False
                    context.report_progress(0.35 + 0.15 * (sheet_no / len(sheets)))
                    if not path:
                        continue
                    sheet = os.path.join(temp_dir, f"{page}.svg")
                    if generate_single_a4_sheet_svg(path, sheet, mark=mark, logger=context.logger):
                        page_svgs.append(sheet)
                if not page_svgs:
                    context.add_warning("Board is too large for an A4 homebrew sheet at 1:1; PDF skipped.")
                    return False
                context.report_progress(0.5)
                # generate_single_a4_sheet_svg always emits portrait A4 pages.
                pdf_ok = export_svg_to_1200dpi_pdf(
                    page_svgs, homebrew_pdf, is_landscape=False, logger=context.logger,
                    should_abort=context.is_aborted, python_exe=context.kicad_python,
                )

            if not pdf_ok:
                context.add_warning(f"Failed to render {PRINT_PDF_DPI} DPI homebrew PDF.")
                return False
            context.report_progress(1.0)
            return True
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class InteractiveBomTask(ExportTask):
    """
    Generate Interactive HTML BOM via InteractiveHtmlBom (pip install if missing).

    Uses a temp copy of the board so pcbnew does not refuse an open file. iBOM
    subprocess env flags are scoped via :func:`ensure_ibom_subprocess_env` only.
    """

    def __init__(self):
        super().__init__("Exporting Interactive HTML BOM")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_ibom", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        if context.is_aborted():
            return False

        ibom_available = False
        ibom_run_cmd = []
        py_exe = context.kicad_python
        
        # Verify InteractiveHtmlBom is available without executing/loading it (avoids pcbnew C++ assertion dialog)
        try:
            subprocess.run(
                [py_exe, "-c", "import sys, importlib.util; sys.exit(0 if importlib.util.find_spec('InteractiveHtmlBom') else 1)"],
                check=True,
                capture_output=True,
                env=context.env,
                startupinfo=context.startupinfo
            )
            ibom_available = True
            ibom_run_cmd = build_ibom_subprocess_command(py_exe)
            context.logger.info("InteractiveHtmlBom successfully verified in python environment.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            if context.is_aborted():
                return False
            context.logger.info("InteractiveHtmlBom not found/working in target Python environment. Attempting to install via pip...")
            if context.progress_callback:
                context.progress_callback(None, None, "Installing InteractiveHtmlBom dependency...")
            
            pip_success = False
            err_output = ""
            
            ibom_pinned = f"InteractiveHtmlBom=={INTERACTIVE_HTML_BOM_PINNED_VERSION}"
            if not context.is_aborted():
                pip_success = self._run_subprocess(
                    [py_exe, "-m", "pip", "install", "--user", ibom_pinned],
                    context,
                )
            if not pip_success and not context.is_aborted():
                context.logger.info("Standard pip install failed. Retrying with --break-system-packages...")
                pip_success = self._run_subprocess(
                    [
                        py_exe,
                        "-m",
                        "pip",
                        "install",
                        "--user",
                        "--break-system-packages",
                        ibom_pinned,
                    ],
                    context,
                )
            if context.is_aborted():
                return False
            if not pip_success:
                err_output = "pip install failed"

            if pip_success:
                try:
                    # Verify installation again using find_spec (without executing)
                    subprocess.run(
                        [py_exe, "-c", "import sys, importlib.util; sys.exit(0 if importlib.util.find_spec('InteractiveHtmlBom') else 1)"],
                        check=True,
                        capture_output=True,
                        env=context.env,
                        startupinfo=context.startupinfo
                    )
                    ibom_available = True
                    ibom_run_cmd = build_ibom_subprocess_command(py_exe)
                    context.logger.info("InteractiveHtmlBom successfully installed and verified via pip.")
                except Exception as verify_err:
                    context.logger.warning(f"Failed to verify InteractiveHtmlBom after installation: {verify_err}")
            else:
                context.logger.warning(f"Failed to install InteractiveHtmlBom via pip. Error details:\n{err_output.strip()}")
                
        if not ibom_available:
            if shutil.which("generate_interactive_bom"):
                ibom_available = True
                ibom_run_cmd = ["generate_interactive_bom"]

        if ibom_available:
            ibom_temp_dir, ibom_input = stage_ibom_project_copy(context)
            ibom_cmd = ibom_run_cmd + build_ibom_cli_args(
                context.options.get("ibom"),
                context.output_dir,
                extra_data_file=ibom_input,
                export_params=context.options,
            ) + [ibom_input]
            try:
                success = self._run_subprocess(
                    ibom_cmd,
                    context,
                    env=ensure_ibom_subprocess_env(context.env),
                )
            finally:
                shutil.rmtree(ibom_temp_dir, ignore_errors=True)
            if context.is_aborted():
                cleanup_partial_ibom_output(context.output_dir, context.pcb_name)
                return False
            if success:
                # Rename default output (ibom.html) to include the versioned board name
                default_ibom = os.path.join(context.output_dir, "ibom.html")
                target_ibom = os.path.join(context.output_dir, f"{context.pcb_name}_ibom.html")
                if os.path.exists(default_ibom):
                    try:
                        shutil.move(default_ibom, target_ibom)
                        context.logger.info(f"Renamed InteractiveHtmlBom output to {os.path.basename(target_ibom)}")
                        
                        # Update the HTML title of the generated page to match the versioned board name.
                        # pcb_name derives from filenames/version tags (untrusted), so it must be
                        # escaped for both the HTML title and the JavaScript string contexts to
                        # avoid HTML/JS injection into the generated report.
                        with open(target_ibom, 'r', encoding='utf-8') as html_f:
                            html_content = html_f.read()

                        title_html = html.escape(context.pcb_name, quote=True)
                        # Function replacement avoids re.sub interpreting backreferences (\1, \g<>)
                        # that could appear in the board name.
                        new_content = re.sub(
                            r'<title>.*?</title>',
                            lambda _m: f'<title>{title_html}</title>',
                            html_content,
                            flags=re.IGNORECASE | re.DOTALL,
                        )

                        # json.dumps produces a safely-quoted, escaped JS string literal.
                        import datetime
                        title_js = json.dumps(context.pcb_name)
                        revision_js = json.dumps(context.version_str or "")
                        date_js = json.dumps(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        override_script = (
                            "\n<script type=\"text/javascript\">\n"
                            "  if (typeof pcbdata !== 'undefined' && pcbdata && pcbdata.metadata) {\n"
                            f"    pcbdata.metadata.title = {title_js};\n"
                            f"    pcbdata.metadata.revision = {revision_js};\n"
                            f"    pcbdata.metadata.date = {date_js};\n"
                            "  }\n"
                            "</script>\n"
                        )
                        if "</body>" in new_content:
                            new_content = new_content.replace("</body>", f"{override_script}</body>")
                        else:
                            new_content += override_script
                        
                        with open(target_ibom, 'w', encoding='utf-8') as html_f:
                            html_f.write(new_content)
                        context.logger.info("Updated InteractiveHtmlBom HTML page title and metadata header.")
                    except Exception as ibom_post_err:
                        context.logger.warning(f"Failed during InteractiveHtmlBom post-processing: {ibom_post_err}")
            return True
        else:
            context.add_warning(
                "Interactive HTML BOM was skipped because InteractiveHtmlBom is not installed."
            )
            return True


class GerberPackTask(ExportTask):
    """Zip ``temp_gerbers/`` into ``{pcb_name}_gerbers.zip`` and remove the staging dir."""

    def __init__(self):
        super().__init__("Zipping Gerber and Drill files")

    def is_applicable(self, context: ExportContext) -> bool:
        return (context.options.get("export_gerbers", True) or context.options.get("export_drills", True)) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        if os.path.exists(context.temp_gerber_dir) and os.listdir(context.temp_gerber_dir):
            gerber_zip_path = os.path.join(context.output_dir, f"{context.pcb_name}_gerbers.zip")
            try:
                # Zip all contents
                with zipfile.ZipFile(gerber_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(context.temp_gerber_dir):
                        for file in files:
                            if file.endswith(GERBER_ZIP_SKIP_SUFFIXES):
                                continue
                            file_full_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_full_path, context.temp_gerber_dir)
                            zipf.write(file_full_path, arcname)

                shutil.rmtree(context.temp_gerber_dir)
            except Exception as e:
                context.logger.error(f"Error packaging Gerbers: {e}", exc_info=True)
                context.add_warning(f"{self.name} failed: {e}")
                return False
        else:
            if os.path.exists(context.temp_gerber_dir):
                shutil.rmtree(context.temp_gerber_dir)
        return True


class BomOutputTask(ExportTask):
    """Rename KiCad BOM export to a versioned filename (unedited KiCad CSV)."""

    def __init__(self):
        super().__init__("Finalizing Bill of Materials")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_bom", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        raw_bom_path = os.path.join(context.output_dir, "raw_bom.csv")
        versioned_bom_path = os.path.join(context.output_dir, f"{context.pcb_name}_bom.csv")
        if not os.path.exists(raw_bom_path):
            context.logger.warning(f"Raw BOM file not found at {raw_bom_path}, skipping BOM finalize.")
            return True
        try:
            if os.path.exists(versioned_bom_path):
                os.remove(versioned_bom_path)
            os.replace(raw_bom_path, versioned_bom_path)
            context.logger.info(f"Saved KiCad BOM: {os.path.basename(versioned_bom_path)}")
        except Exception as e:
            context.logger.error(f"Error finalizing BOM: {e}", exc_info=True)
            context.add_warning(f"{self.name} failed: {e}")
            return False
        return True


class PosOutputTask(ExportTask):
    """Rename KiCad placement export to a versioned filename (unedited KiCad CSV)."""

    def __init__(self):
        super().__init__("Finalizing Component Placement")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_pos", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        raw_pos_path = os.path.join(context.output_dir, "raw_pos.csv")
        versioned_pos_path = os.path.join(context.output_dir, f"{context.pcb_name}_pos.csv")
        if not os.path.exists(raw_pos_path):
            context.logger.warning(f"Raw position file not found at {raw_pos_path}, skipping CPL finalize.")
            return True
        try:
            if os.path.exists(versioned_pos_path):
                os.remove(versioned_pos_path)
            os.replace(raw_pos_path, versioned_pos_path)
            context.logger.info(f"Saved KiCad placement: {os.path.basename(versioned_pos_path)}")
        except Exception as e:
            context.logger.error(f"Error finalizing placement: {e}", exc_info=True)
            context.add_warning(f"{self.name} failed: {e}")
            return False
        return True


class JlcFormatTask(ExportTask):
    """Produce JLC-ready BOM/CPL from KiCad CSV exports."""

    def __init__(self):
        super().__init__("Generating JLCPCB BOM/CPL")

    def is_applicable(self, context: ExportContext) -> bool:
        if not context.options.get("format_jlc", True):
            return False
        if not (context.options.get("export_bom", True) or context.options.get("export_pos", True)):
            return False
        return bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        if context.is_aborted():
            return False
        return format_jlc_exports(context)


# ---------------------------------------------------------------------------
# Pipeline orchestration & public API
# ---------------------------------------------------------------------------

class ExportRunner:
    """
    Run the ordered export pipeline for a resolved :class:`ExportContext`.

    Skips tasks where :meth:`ExportTask.is_applicable` is false. Individual step
    failures add warnings and do not stop the run unless every step fails or the
    user cancels via :meth:`ExportContext.cancel`.
    """
    
    def __init__(self, context: ExportContext):
        self.context = context
        self.tasks = []
        self._initialize_pipeline()

    def _initialize_pipeline(self):
        # 1. Main CLI export commands
        self.tasks.append(GerberExportTask())
        self.tasks.append(DrillExportTask())
        self.tasks.append(PlacementExportTask())
        self.tasks.append(BomExportTask())
        self.tasks.append(SchematicPdfExportTask())
        self.tasks.append(Step3dExportTask())
        self.tasks.append(Render3dExportTask())
        self.tasks.append(SvgExportTask())
        self.tasks.append(HomebrewPdfExportTask())
        self.tasks.append(InteractiveBomTask())
        
        # 2. Post-processing: version KiCad BOM/POS; optional JLC copies
        self.tasks.append(GerberPackTask())
        self.tasks.append(BomOutputTask())
        self.tasks.append(PosOutputTask())
        self.tasks.append(JlcFormatTask())

    def _is_applicable(self, task: "ExportTask") -> bool:
        """Evaluate task applicability without letting a faulty check abort the pipeline."""
        try:
            return task.is_applicable(self.context)
        except Exception as exc:
            self.context.logger.error(
                "Applicability check for '%s' failed: %s", task.name, exc, exc_info=True
            )
            self.context.add_warning(f"{task.name} was skipped: {exc}")
            return False

    def execute(self) -> bool:
        """Run all applicable tasks; continue after individual step failures."""
        applicable_tasks = [t for t in self.tasks if self._is_applicable(t)]
        total_steps = len(applicable_tasks)
        tasks_succeeded = 0

        self.context.logger.info(f"Running KiForge pipeline with {total_steps} tasks.")

        for idx, task in enumerate(applicable_tasks):
            if self.context.is_aborted():
                self._cleanup_temp_dirs()
                return False

            self.context.begin_step(idx, total_steps)
            if self.context.progress_callback:
                msg = f"Running: {task.name}..."
                keep_going = self.context.progress_callback(idx, total_steps, msg)
                if not keep_going:
                    self.context.cancel()
                    self._cleanup_temp_dirs()
                    return False

            try:
                success = task.run(self.context)
            except Exception as exc:
                self.context.logger.error(
                    "Task '%s' failed with exception: %s", task.name, exc, exc_info=True
                )
                self.context.add_warning(f"{task.name} failed: {exc}")
                success = False

            if success:
                tasks_succeeded += 1

            if self.context.is_aborted():
                break

        self._cleanup_temp_dirs()

        if self.context.is_aborted():
            if self.context.progress_callback:
                self.context.progress_callback(total_steps, total_steps, "Export cancelled.")
            return False

        if total_steps > 0 and tasks_succeeded == 0:
            self.context.add_warning("No export steps completed successfully.")
            if self.context.progress_callback:
                self.context.progress_callback(total_steps, total_steps, "Export failed.")
            return False

        if self.context.progress_callback:
            if self.context.warnings:
                self.context.progress_callback(
                    total_steps, total_steps, "Completed with warnings."
                )
            else:
                self.context.progress_callback(total_steps, total_steps, "Completed successfully!")

        if self.context.warnings:
            self.context.logger.warning(
                "KiForge export completed with %d warning(s).", len(self.context.warnings)
            )
        else:
            self.context.logger.info("KiForge Exporter pipeline executed successfully.")
        return True

    def _cleanup_temp_dirs(self):
        """Cleans up temporary workspace directories on error or abort"""
        temp_dir = getattr(self.context, "temp_gerber_dir", None)
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
        if self.context.is_aborted() and self.context.output_dir:
            cleanup_partial_ibom_output(self.context.output_dir, self.context.pcb_name)


def generate_cd_files(project_dir: str, output_dir_name: str, options: dict) -> tuple[str, bool]:
    """
    Write GitHub and Gitea release workflow YAML and sync project ``.gitignore``.

    Templates are loaded from ``templates/github-release.yml`` and
    ``templates/gitea-release.yml`` with export toggles substituted. Returns
    ``(user_message, success)``.
    """
    github_dir = os.path.join(project_dir, ".github", "workflows")
    gitea_dir = os.path.join(project_dir, ".gitea", "workflows")
    try:
        # 1. GitHub Actions Release Workflow (from templates/github-release.yml)
        os.makedirs(github_dir, exist_ok=True)
        github_yaml_path = os.path.join(github_dir, "release.yml")
        github_yaml_content = render_cd_workflow_template(
            "github-release.yml", output_dir_name, options
        )
        with open(github_yaml_path, 'w', encoding='utf-8') as f:
            f.write(github_yaml_content)

        # 2. Gitea Actions Release Workflow (from templates/gitea-release.yml)
        os.makedirs(gitea_dir, exist_ok=True)
        gitea_yaml_path = os.path.join(gitea_dir, "release.yml")
        gitea_yaml_content = render_cd_workflow_template(
            "gitea-release.yml", output_dir_name, options
        )
        with open(gitea_yaml_path, 'w', encoding='utf-8') as f:
            f.write(gitea_yaml_content)
            
        gitignore_updated = update_project_gitignore(project_dir, output_dir_name)
            
        msg = (
            f"CD workflows generated successfully:\n"
            f"  - GitHub: .github/workflows/release.yml\n"
            f"  - Gitea: .gitea/workflows/release.yml"
        )
        if gitignore_updated:
            template_hint = get_gitignore_template_path()
            if template_hint:
                msg += (
                    f"\n\nKiCad & KiForge ignore patterns added/updated in .gitignore "
                    f"(template: {template_hint})."
                )
            else:
                msg += f"\n\nKiCad & KiForge ignore patterns added/updated in .gitignore."
        else:
            msg += f"\n\nAll KiCad & KiForge patterns were already ignored in .gitignore."
        return msg, True
    except Exception as e:
        return f"Failed to generate CD files: {e}", False


# Deprecated alias; prefer generate_cd_files.
generate_ci_files = generate_cd_files


def run_export(project_path=None, output_dir=None, export_3d=True, export_svg=True, export_print_pdf=True, export_bom=True, export_sch_pdf=True, export_pos=True, export_step=True, export_gerbers=True, export_drills=True, export_ibom=True, progress_callback=None, context=None):
    """
    Main library entry point for CLI, Studio, and CD workflows.

    Pass a pre-resolved ``context`` (after ``context.resolve()``) from the GUI,
    or supply ``project_path`` / ``output_dir`` and boolean export flags for the
    legacy procedural API. When ``generate_cd`` is enabled and the run succeeds,
    CD workflow files and ``.gitignore`` are updated to match the export toggles
    (skipped inside GitHub Actions itself).
    """
    if context is None:
        options = apply_export_runtime_options(apply_export_params_to_options({
            "export_3d": export_3d,
            "export_svg": export_svg,
            "export_print_pdf": export_print_pdf,
            "export_bom": export_bom,
            "export_sch_pdf": export_sch_pdf,
            "export_pos": export_pos,
            "export_step": export_step,
            "export_gerbers": export_gerbers,
            "export_drills": export_drills,
            "export_ibom": export_ibom,
        }))

        context = ExportContext(project_path, output_dir, options, progress_callback)
        if not context.resolve():
            return False
    else:
        context.options = apply_export_runtime_options(apply_export_params_to_options(context.options))

    runner = ExportRunner(context)
    success = runner.execute()

    generate_cd = context.options.get("generate_cd", context.options.get("generate_ci", True))
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        if generate_cd:
            context.logger.info(
                "Skipping CD workflow generation inside GitHub Actions (workflows already present)."
            )
        generate_cd = False
    if success and generate_cd:
        cd_options = export_options_from_context(context)
        cd_msg, cd_ok = generate_cd_files(context.project_dir, context.output_dir_name, cd_options)
        if cd_ok:
            context.logger.info(f"CD workflow files updated: {cd_msg.replace(chr(10), ' ')}")
        else:
            context.add_warning(f"CD workflow generation failed: {cd_msg}")

    return success


def print_export_summary(context: ExportContext, success: bool) -> None:
    """Print a concise CLI summary after export completes."""
    if success and context.warnings:
        print("\n[KiForge] Export completed with warnings:", file=sys.stderr)
        for warning in context.warnings:
            print(f"  - {warning}", file=sys.stderr)
    elif not success:
        print("\n[KiForge] Export failed.", file=sys.stderr)
        for warning in context.warnings:
            print(f"  - {warning}", file=sys.stderr)


def parse_cli_args(args=None):
    """
    Parse CLI arguments for ``python kiforge.py``.

    Export toggles are generated from EXPORT_SETTING_KEYS. Placement/STEP flags
    are generated from EXPORT_PARAM_SPECS (plus ``--top`` / ``--bottom`` aliases).
    Runtime flags come from RUNTIME_OPTION_SPECS. BOM and 3D render behavior is
    not configurable on the CLI.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="KiForge - KiCad 10 Exporter CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project-path", "--project_path", dest="project_path", default=".")
    parser.add_argument("--output-dir", "--output_dir", dest="output_dir", default="kiforge")
    for key in EXPORT_SETTING_KEYS:
        if key == "generate_cd":
            continue
        flag = f"--{key.replace('_', '-')}"
        parser.add_argument(flag, action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--version-tag",
        "--version_tag",
        dest="version_tag",
        default=None,
        help="Version tag to append to output filenames",
    )
    for spec in EXPORT_PARAM_SPECS:
        kw = {"dest": spec["key"], "default": None, "help": spec["help"]}
        if spec["type"] == "bool":
            parser.add_argument(spec["cli"], action=argparse.BooleanOptionalAction, **kw)
        elif spec["type"] == "choice":
            parser.add_argument(spec["cli"], choices=spec["choices"], **kw)
        elif spec["type"] == "float":
            parser.add_argument(spec["cli"], type=float, **kw)
        elif spec["type"] == "int":
            parser.add_argument(spec["cli"], type=int, **kw)
        else:
            parser.add_argument(spec["cli"], type=str, **kw)
    parser.add_argument(
        "--top",
        action="store_const",
        const="front",
        dest="pos_side",
        default=None,
        help="Placement CSV: top side only (alias for --pos-side front)",
    )
    parser.add_argument(
        "--bottom",
        action="store_const",
        const="back",
        dest="pos_side",
        default=None,
        help="Placement CSV: bottom side only (alias for --pos-side back)",
    )
    for spec in RUNTIME_OPTION_SPECS:
        parser.add_argument(
            spec["cli"],
            action=argparse.BooleanOptionalAction,
            dest=spec["key"],
            default=DEFAULT_EXPORT_RUNTIME_OPTIONS[spec["key"]],
            help=spec["help"],
        )
    parser.add_argument(
        "--generate-cd",
        action="store_true",
        dest="generate_cd",
        help="Generate GitHub/Gitea release CD workflow and update .gitignore instead of exporting",
    )
    parser.add_argument("--generate-ci", action="store_true", dest="generate_cd", help=argparse.SUPPRESS)
    return parser.parse_args(args)


def export_params_from_cli_args(args) -> dict | None:
    """
    Collect export_params keys explicitly set on the CLI.

    Omitted flags return None so :func:`build_cli_options` does not override
    values loaded from ``.kiforge.json`` during a normal export run.
    """
    params = {}
    for spec in EXPORT_PARAM_SPECS:
        value = getattr(args, spec["key"], None)
        if value is not None:
            params[spec["key"]] = value
    return params or None


def runtime_options_from_cli_args(args) -> dict:
    """Collect per-run runtime flags from parsed CLI arguments."""
    runtime = {}
    for spec in RUNTIME_OPTION_SPECS:
        runtime[spec["key"]] = getattr(args, spec["key"])
    return apply_export_runtime_options(runtime)


def build_cli_options(args, *, flatten_params: bool = False) -> dict:
    """
    Build an options dict from parsed CLI arguments.

    When ``flatten_params`` is False (normal export), only CLI-provided
    export_params are attached so :meth:`ExportContext.resolve` can merge
    project/global ``.kiforge.json``. When True (``--generate-cd``), defaults
    are flattened like Studio/CD workflow generation expects.
    """
    options = {
        key: getattr(args, key, DEFAULT_EXPORT_SETTINGS[key])
        for key in EXPORT_SETTING_KEYS
        if key != "generate_cd"
    }
    options["version"] = args.version_tag
    export_params = export_params_from_cli_args(args)
    if export_params:
        options["export_params"] = merge_export_params(None, export_params)
    options.update(runtime_options_from_cli_args(args))
    if flatten_params:
        return apply_export_params_to_options(options)
    return options


if __name__ == "__main__":
    setup_logger()
    args = parse_cli_args()
    
    if args.generate_cd:
        options = build_cli_options(args, flatten_params=True)
        msg, success = generate_cd_files(args.project_path, args.output_dir, options)
        print(msg)
        sys.exit(0 if success else 1)
        
    try:
        options = build_cli_options(args)
        
        context = ExportContext(args.project_path, args.output_dir, options)
        if not context.resolve():
            sys.exit(1)
            
        success = run_export(context=context)
        print_export_summary(context, success)
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Export aborted by user (KeyboardInterrupt).")
        print("\n[KiForge] Export aborted by user.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        print(f"\n[KiForge] Export failed: {e}", file=sys.stderr)
        sys.exit(1)
