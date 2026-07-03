#!/usr/bin/env python3
"""
KiForge — KiCad 10 Manufacturing & Documentation Exporter
==========================================================

Single source of truth for manufacturing and documentation exports. KiForge runs
``kicad-cli`` in a structured pipeline, optionally post-processes BOM and placement
CSVs for JLCPCB, and can generate GitHub/Gitea release workflows for downstream
KiCad projects.

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
CLI/GUI flags. Saved JSON uses nested ``exports`` and ``ibom`` groups; legacy flat
keys and ``generate_ci`` are still accepted.

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
JLCPCBFormatter  Stateless BOM/CPL column reformatting for JLCPCB upload.
ExportTask       Abstract export step; subclasses implement ``is_applicable`` / ``run``.
ExportRunner     Ordered pipeline driver with progress and cleanup.
generate_cd_files  Write CD workflow YAML and update project ``.gitignore``.
"""

import os
import sys
import csv
import html
import zipfile
import shutil
import tempfile
import subprocess
import logging
import site
import threading
import json
import re

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

# Default composite-action reference for generated CD workflows (branch until stable tag).
KIFORGE_ACTION_REF = "alphaseneca/kiforge@main"

# ---------------------------------------------------------------------------
# Defaults & persisted settings
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
    "format_jlc": True,
    "generate_cd": True,
}

EXPORT_SETTING_KEYS = tuple(DEFAULT_EXPORT_SETTINGS.keys())

# Set only when starting an export run (CLI, Studio Run Export, GitHub Action).
# Never loaded from or saved to .kiforge.json / settings.json.
DEFAULT_EXPORT_RUNTIME_OPTIONS = {
    "sync_title_block_rev": True,
}


def apply_export_runtime_options(options: dict | None) -> dict:
    """Attach export-only runtime flags (not persisted settings)."""
    merged = dict(options or {})
    for key, default in DEFAULT_EXPORT_RUNTIME_OPTIONS.items():
        merged.setdefault(key, default)
    return merged

DEFAULT_SETTINGS = {
    "output_dir": "kiforge",
    **DEFAULT_EXPORT_SETTINGS,
}

DEFAULT_IBOM_SETTINGS = {
    "include_tracks": False,
    "include_netlist": False,
    "dark_mode": False,
    "checkboxes": False,
    "show_fabrication": False,
    "hide_pads": False,
    "highlight_pin1": True,
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


def render_cd_workflow_template(template_name: str, output_dir_name: str, options: dict) -> str:
    """
    Load a CD workflow YAML template and substitute export options.

    Template placeholders use {{NAME}} syntax (see templates/github-release.yml).
    """
    template_path = require_template_path(template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    substitutions = {
        "OUTPUT_DIR": output_dir_name,
        "EXPORT_3D": _cd_option_str(options, "export_3d"),
        "EXPORT_SVG": _cd_option_str(options, "export_svg"),
        "EXPORT_BOM": _cd_option_str(options, "export_bom"),
        "EXPORT_SCH_PDF": _cd_option_str(options, "export_sch_pdf"),
        "EXPORT_POS": _cd_option_str(options, "export_pos"),
        "EXPORT_STEP": _cd_option_str(options, "export_step"),
        "EXPORT_GERBERS": _cd_option_str(options, "export_gerbers"),
        "EXPORT_DRILLS": _cd_option_str(options, "export_drills"),
        "EXPORT_IBOM": _cd_option_str(options, "export_ibom"),
        "FORMAT_JLC": _cd_option_str(options, "format_jlc"),
        "KIFORGE_ACTION_REF": KIFORGE_ACTION_REF,
        "GITHUB_REF_NAME": "${{ github.ref_name }}",
    }
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


def _coerce_setting_value(default, value):
    if isinstance(default, bool) and isinstance(value, str):
        return value.lower() == "true"
    return value


# ---------------------------------------------------------------------------
# iBOM (Interactive HTML BOM) integration
# ---------------------------------------------------------------------------

def merge_ibom_settings(base: dict | None, overlay: dict | None) -> dict:
    """Merge iBOM option dicts (overlay wins)."""
    merged = DEFAULT_IBOM_SETTINGS.copy()
    if base:
        for key in DEFAULT_IBOM_SETTINGS:
            if key in base:
                merged[key] = _coerce_setting_value(DEFAULT_IBOM_SETTINGS[key], base[key])
    if overlay:
        for key in DEFAULT_IBOM_SETTINGS:
            if key in overlay:
                merged[key] = _coerce_setting_value(DEFAULT_IBOM_SETTINGS[key], overlay[key])
    return merged


def build_ibom_cli_args(ibom_settings: dict | None, output_dir: str) -> list[str]:
    """Build InteractiveHtmlBom CLI flags from saved/default iBOM settings."""
    settings = merge_ibom_settings(None, ibom_settings)
    args = []
    flag_map = {
        "include_tracks": "--include-tracks",
        "include_netlist": "--include-nets",
        "dark_mode": "--dark-mode",
        "checkboxes": "--checkboxes",
        "show_fabrication": "--show-fabrication",
        "hide_pads": "--hide-pads",
        "highlight_pin1": "--highlight-pin1",
    }
    for key, flag in flag_map.items():
        if settings.get(key):
            args.append(flag)
    # KiForge batch export must never launch a browser (including on cancel races).
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

    Must use ``python -m InteractiveHtmlBom.generate_interactive_bom`` — importing the
    package via ``python -c`` makes iBOM register a pcbnew ActionPlugin and fails when
    KiCad is not running that interpreter as the main program.
    """
    return [python_executable, "-m", "InteractiveHtmlBom.generate_interactive_bom"]


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
    Copy the open board (and minimal project files) to a temp folder.

    pcbnew refuses to load a board that is already open in the PCB editor; a temp copy
    avoids touching the live project lock file.
    """
    ibom_temp_dir = tempfile.mkdtemp(prefix="kiforge_ibom_")
    pcb_basename = os.path.basename(context.pcb_file)
    names_to_copy = {pcb_basename}
    for suffix in (".kicad_pro", ".kicad_sch", ".kicad_prl"):
        candidate = f"{context.pcb_name}{suffix}"
        if os.path.isfile(os.path.join(context.project_dir, candidate)):
            names_to_copy.add(candidate)
    for name in sorted(names_to_copy):
        shutil.copy2(
            os.path.join(context.project_dir, name),
            os.path.join(ibom_temp_dir, name),
        )
    return ibom_temp_dir, os.path.join(ibom_temp_dir, pcb_basename)


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

    settings["ibom"] = merge_ibom_settings(settings.get("ibom"), loaded.get("ibom"))

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
    settings["ibom"] = DEFAULT_IBOM_SETTINGS.copy()
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
    get_global_settings_path(). Export toggles are stored under ``exports``;
    iBOM options under ``ibom``.

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
        "ibom": merge_ibom_settings(None, settings.get("ibom")),
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
    matches the same toggles the user selected in the GUI or CLI (Gerbers, STEP,
    format_jlc, generate_cd, and so on). Legacy generate_ci
    option keys are mapped to generate_cd for backward compatibility.

    Args:
        context: A resolved ExportContext from the current run.

    Returns:
        dict: Option flags suitable for generate_cd_files().
    """
    keys = (
        "export_gerbers", "export_drills", "export_pos", "export_bom", "export_ibom",
        "export_sch_pdf", "export_step", "export_3d", "export_svg", "format_jlc",
        "generate_cd", "version",
    )
    options = {key: context.options.get(key, DEFAULT_SETTINGS.get(key, True)) for key in keys}
    if "generate_cd" not in context.options and context.options.get("generate_ci") is not None:
        options["generate_cd"] = context.options.get("generate_ci")
    return options


def _build_subprocess_env(kicad_cli: str | None) -> dict:
    """
    Build the environment passed to kicad-cli and helper subprocesses.

    Ensures user site-packages and KiCad PCM paths are on PYTHONPATH and prepends
    the KiCad bin directory to PATH. iBOM-specific flags are applied separately via
    ensure_ibom_subprocess_env() only when launching InteractiveHtmlBom.
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

    return env


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
        self.env = _build_subprocess_env(self.kicad_cli)

        if not self._discover_project_files():
            return False

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

        self.options["ibom"] = merge_ibom_settings(
            merged_settings.get("ibom"),
            self.options.get("ibom"),
        )

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
# JLCPCB BOM/CPL post-processing
# ---------------------------------------------------------------------------

class JLCPCBFormatter:
    """
    Convert raw KiCad CSV exports into JLCPCB upload format.

    :meth:`format_bom` filters DNP rows and normalizes LCSC column aliases.
    :meth:`format_cpl` maps placement columns and applies per-footprint rotation
    offsets from ``ExportContext.rotation_offsets``.
    """
    
    @staticmethod
    def format_bom(raw_bom_path: str, output_bom_path: str) -> None:
        """Converts raw KiCad BOM to the JLCPCB format with LCSC Part Numbers resolved and DNP filtered"""
        if not os.path.exists(raw_bom_path):
            return
            
        with open(raw_bom_path, 'r', newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            
        jlc_rows = []
        lcsc_aliases = ['LCSC', 'LCSC Part', 'LCSC Part #', 'JLCPCB Part', 'JLCPCB Part #', 'LCSC_Part']
        
        for row in rows:
            dnp = row.get('${DNP}', '').strip().lower() or row.get('DNP', '').strip().lower()
            if dnp in ['1', 'dnp', 'true', 'yes']:
                continue
                
            designator = row.get('Reference', '').strip() or row.get('Designator', '').strip()
            comment = row.get('Value', '').strip() or row.get('Comment', '').strip()
            footprint = row.get('Footprint', '').strip()
            qty = row.get('${QUANTITY}', '').strip() or row.get('QUANTITY', '').strip() or row.get('Quantity', '').strip() or row.get('Qty', '1').strip()
            
            lcsc_val = ''
            for alias in lcsc_aliases:
                if alias in row and row[alias]:
                    lcsc_val = row[alias].strip()
                    break
                    
            jlc_rows.append({
                'Designator': designator,
                'Comment': comment,
                'Footprint': footprint,
                'LCSC': lcsc_val,
                'Quantity': qty
            })
            
        with open(output_bom_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['Designator', 'Comment', 'Footprint', 'LCSC', 'Quantity'])
            writer.writeheader()
            writer.writerows(jlc_rows)

    @staticmethod
    def format_cpl(raw_pos_path: str, output_cpl_path: str, rotation_offsets: dict = None) -> None:
        """Converts raw KiCad position file to the JLCPCB CPL format and applies rotation offsets"""
        if not os.path.exists(raw_pos_path):
            return
            
        with open(raw_pos_path, 'r', newline='', encoding='utf-8-sig') as f:
            lines = f.readlines()
            
        clean_lines = [line for line in lines if not line.strip().startswith('#')]
        reader = csv.DictReader(clean_lines)
        rows = list(reader)
        
        offsets = dict(rotation_offsets or {})
        
        jlc_cpl_rows = []
        for row in rows:
            ref = row.get('Ref', '').strip()
            val = row.get('Val', '').strip()
            package = row.get('Package', '').strip()
            pos_x = row.get('PosX', '').strip()
            pos_y = row.get('PosY', '').strip()
            rot_str = row.get('Rot', '').strip()
            side = row.get('Side', '').strip()
            
            try:
                rotation = float(rot_str)
            except ValueError:
                rotation = 0.0
                
            for pattern, offset in offsets.items():
                if pattern.lower() in package.lower() or pattern.lower() in val.lower():
                    rotation = (rotation + offset) % 360.0
                    break
                    
            layer = 'Bottom' if side.lower() in ['bottom', 'back', 'b.cu'] else 'Top'
                
            jlc_cpl_rows.append({
                'Designator': ref,
                'Mid X': pos_x,
                'Mid Y': pos_y,
                'Layer': layer,
                'Rotation': f"{rotation:.2f}" if rotation % 1 != 0 else f"{int(rotation)}"
            })
            
        with open(output_cpl_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
            writer.writeheader()
            writer.writerows(jlc_cpl_rows)


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

    def _run_subprocess(self, cmd: list, context: ExportContext, *, env=None) -> bool:
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
                format_task_failure_message(self.name, e.stderr or "", e.stdout or "", cmd)
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


class GerberExportTask(ExportTask):
    """Export PCB copper and mask layers to ``temp_gerbers/`` via kicad-cli."""

    def __init__(self):
        super().__init__("Exporting Gerber Layers")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_gerbers", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        cmd = [
            context.kicad_cli, "pcb", "export", "gerbers",
            "--use-drill-file-origin",
            "-o", context.temp_gerber_dir,
            context.pcb_file
        ]
        return self._run_subprocess(cmd, context)


class DrillExportTask(ExportTask):
    """Export Excellon drill files; runs when drills or gerbers are enabled."""
    def __init__(self):
        super().__init__("Exporting Drill Files")

    def is_applicable(self, context: ExportContext) -> bool:
        drills_requested = context.options.get("export_drills", True)
        gerbers_requested = context.options.get("export_gerbers", True)
        return (drills_requested or gerbers_requested) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        cmd = [
            context.kicad_cli, "pcb", "export", "drill",
            "--excellon-separate-th",
            "--excellon-units", "mm",
            "--drill-origin", "plot",
            "-o", context.temp_gerber_dir,
            context.pcb_file
        ]
        return self._run_subprocess(cmd, context)


class PlacementExportTask(ExportTask):
    """Write KiCad placement CSV to ``raw_pos.csv`` in the output directory."""
    def __init__(self):
        super().__init__("Exporting Position Data")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_pos", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        raw_pos_path = os.path.join(context.output_dir, "raw_pos.csv")
        cmd = [
            context.kicad_cli, "pcb", "export", "pos",
            "--format", "csv",
            "--exclude-dnp",
            "--use-drill-file-origin",
            "--units", "mm",
            context.pcb_file,
            "-o", raw_pos_path
        ]
        return self._run_subprocess(cmd, context)


class BomExportTask(ExportTask):
    """Write KiCad BOM CSV to ``raw_bom.csv`` (includes LCSC field aliases)."""
    def __init__(self):
        super().__init__("Exporting Bill of Materials")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_bom", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        raw_bom_path = os.path.join(context.output_dir, "raw_bom.csv")
        cmd = [
            context.kicad_cli, "sch", "export", "bom",
            "--fields", "Reference,Value,Footprint,Description,${QUANTITY},${DNP},LCSC,LCSC Part,LCSC Part #,JLCPCB Part,JLCPCB Part #,ID",
            "--group-by", "Value,Footprint,LCSC,LCSC Part,LCSC Part #,JLCPCB Part,JLCPCB Part #,${DNP},ID",
            "--ref-range-delimiter", "",
            context.sch_file, "-o", raw_bom_path
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
    """Export ``{pcb_name}.step``; treats non-fatal KiCad model warnings as partial success."""
    def __init__(self):
        super().__init__("Exporting STEP 3D Model")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_step", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        """Export STEP; keep partial output when KiCad reports non-fatal model warnings."""
        output_step = os.path.join(context.output_dir, f"{context.pcb_name}.step")
        cmd = [
            context.kicad_cli, "pcb", "export", "step",
            "--subst-models",
            "-f",
            "-o", output_step,
            context.pcb_file
        ]
        if self._run_subprocess(cmd, context):
            return True
        if os.path.isfile(output_step) and os.path.getsize(output_step) > 0:
            context.add_warning(
                f"{self.name} finished with warnings; a partial STEP file was still saved."
            )
            return True
        return False


class Render3dExportTask(ExportTask):
    """Render front and back 3D PNG views of the board."""
    def __init__(self):
        super().__init__("Rendering 3D Views")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_3d", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        # Render Front
        front_png = os.path.join(context.output_dir, f"{context.pcb_name}_3d_front.png")
        cmd_front = [
            context.kicad_cli, "pcb", "render", context.pcb_file,
            "--output", front_png,
            "--rotate", "0,0,0", "--preset", "2", "--floor", "--perspective",
            "--zoom", "0.8", "--quality", "high", "--width", "1920", "--height", "1080"
        ]
        ok_front = self._run_subprocess(cmd_front, context)
        if not ok_front:
            context.logger.warning("Front 3D render failed; attempting back view anyway.")

        # Render Back
        back_png = os.path.join(context.output_dir, f"{context.pcb_name}_3d_back.png")
        cmd_back = [
            context.kicad_cli, "pcb", "render", context.pcb_file,
            "--output", back_png,
            "--rotate", "0,180,0", "--preset", "2", "--floor", "--perspective",
            "--zoom", "0.8", "--quality", "high", "--width", "1920", "--height", "1080"
        ]
        ok_back = self._run_subprocess(cmd_back, context)
        return ok_front or ok_back


class SvgExportTask(ExportTask):
    """Export front and back copper SVG previews (``{pcb_name}_front/back.svg``)."""

    def __init__(self):
        super().__init__("Exporting Vector SVGs")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_svg", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        # Front SVG
        front_svg = os.path.join(context.output_dir, f"{context.pcb_name}_front.svg")
        cmd_front = [
            context.kicad_cli, "pcb", "export", "svg",
            "-l", "F.Cu,Edge.Cuts", "-n", "--drill-shape-opt", "2",
            "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
            "--output", front_svg,
            "--black-and-white", context.pcb_file
        ]
        ok_front = self._run_subprocess(cmd_front, context)
        if not ok_front:
            context.logger.warning("Front SVG export failed; attempting back layer export anyway.")

        # Back SVG
        back_svg = os.path.join(context.output_dir, f"{context.pcb_name}_back.svg")
        cmd_back = [
            context.kicad_cli, "pcb", "export", "svg",
            "-l", "B.Cu,Edge.Cuts", "-m", "-n", "--drill-shape-opt", "2",
            "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
            "--output", back_svg,
            "--black-and-white", context.pcb_file
        ]
        ok_back = self._run_subprocess(cmd_back, context)
        return ok_front or ok_back


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
            
            if not context.is_aborted():
                pip_success = self._run_subprocess(
                    [py_exe, "-m", "pip", "install", "--user", "InteractiveHtmlBom"],
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
                        "InteractiveHtmlBom",
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
                context.options.get("ibom"), context.output_dir
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
                        title_js = json.dumps(context.pcb_name)
                        override_script = (
                            "\n<script type=\"text/javascript\">\n"
                            "  if (typeof pcbdata !== 'undefined' && pcbdata && pcbdata.metadata) {\n"
                            f"    pcbdata.metadata.title = {title_js};\n"
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
    """Rename KiCad BOM export to a versioned filename and optionally produce JLCPCB CSV."""

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
            if context.options.get("format_jlc", True):
                jlc_bom_path = os.path.join(context.output_dir, f"{context.pcb_name}_bom_jlc.csv")
                JLCPCBFormatter.format_bom(versioned_bom_path, jlc_bom_path)
                context.logger.info(f"Saved JLCPCB BOM: {os.path.basename(jlc_bom_path)}")
        except Exception as e:
            context.logger.error(f"Error finalizing BOM: {e}", exc_info=True)
            context.add_warning(f"{self.name} failed: {e}")
            return False
        return True


class PosOutputTask(ExportTask):
    """Rename KiCad placement export to a versioned filename and optionally produce JLCPCB CPL."""

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
            if context.options.get("format_jlc", True):
                jlc_cpl_path = os.path.join(context.output_dir, f"{context.pcb_name}_cpl_jlc.csv")
                JLCPCBFormatter.format_cpl(versioned_pos_path, jlc_cpl_path, context.rotation_offsets)
                context.logger.info(f"Saved JLCPCB CPL: {os.path.basename(jlc_cpl_path)}")
        except Exception as e:
            context.logger.error(f"Error finalizing placement: {e}", exc_info=True)
            context.add_warning(f"{self.name} failed: {e}")
            return False
        return True


# Legacy names kept for external importers and older tests.
JlcBomFormatTask = BomOutputTask
JlcCplFormatTask = PosOutputTask


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
        self.tasks.append(InteractiveBomTask())
        
        # 2. Post-processing: version raw BOM/POS and optionally emit JLCPCB CSVs
        self.tasks.append(GerberPackTask())
        self.tasks.append(BomOutputTask())
        self.tasks.append(PosOutputTask())

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


def run_export(project_path=None, output_dir=None, export_3d=True, export_svg=True, export_bom=True, export_sch_pdf=True, export_pos=True, export_step=True, export_gerbers=True, export_drills=True, export_ibom=True, progress_callback=None, context=None):
    """
    Main library entry point for CLI, Studio, and CD workflows.

    Pass a pre-resolved ``context`` (after ``context.resolve()``) from the GUI,
    or supply ``project_path`` / ``output_dir`` and boolean export flags for the
    legacy procedural API. When ``generate_cd`` is enabled and the run succeeds,
    CD workflow files and ``.gitignore`` are updated to match the export toggles
    (skipped inside GitHub Actions itself).
    """
    if context is None:
        options = apply_export_runtime_options({
            "export_3d": export_3d,
            "export_svg": export_svg,
            "export_bom": export_bom,
            "export_sch_pdf": export_sch_pdf,
            "export_pos": export_pos,
            "export_step": export_step,
            "export_gerbers": export_gerbers,
            "export_drills": export_drills,
            "export_ibom": export_ibom,
        })

        context = ExportContext(project_path, output_dir, options, progress_callback)
        if not context.resolve():
            return False
    else:
        context.options = apply_export_runtime_options(context.options)

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
    """Parse CLI arguments for ``python kiforge.py`` (export and ``--generate-cd`` modes)."""
    import argparse
    parser = argparse.ArgumentParser(description="KiForge - KiCad 10 Exporter CLI")
    parser.add_argument("--project-path", "--project_path", dest="project_path", default=".")
    parser.add_argument("--output-dir", "--output_dir", dest="output_dir", default="kiforge")
    parser.add_argument("--export-3d", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-svg", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-bom", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-sch-pdf", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-pos", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-step", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-gerbers", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-drills", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-ibom", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--format-jlc", action=argparse.BooleanOptionalAction, default=True,
                        help="Apply JLCPCB BOM/CPL column formatting and rotation offsets")
    parser.add_argument(
        "--sync-title-block-rev",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Sync schematic title-block (rev) to export version via staged copy (export runs only)",
    )
    parser.add_argument("--version-tag", "--version_tag", dest="version_tag", default=None, help="Version tag to append to output filenames")
    parser.add_argument("--generate-cd", action="store_true", dest="generate_cd",
                        help="Generate GitHub/Gitea release CD workflow and update .gitignore instead of exporting")
    parser.add_argument("--generate-ci", action="store_true", dest="generate_cd", help=argparse.SUPPRESS)
    return parser.parse_args(args)


if __name__ == "__main__":
    setup_logger()
    args = parse_cli_args()
    
    if args.generate_cd:
        options = {
            "export_3d": args.export_3d,
            "export_svg": args.export_svg,
            "export_bom": args.export_bom,
            "export_sch_pdf": args.export_sch_pdf,
            "export_pos": args.export_pos,
            "export_step": args.export_step,
            "export_gerbers": args.export_gerbers,
            "export_drills": args.export_drills,
            "export_ibom": args.export_ibom,
            "format_jlc": args.format_jlc,
            "version": args.version_tag
        }
        msg, success = generate_cd_files(args.project_path, args.output_dir, options)
        print(msg)
        sys.exit(0 if success else 1)
        
    try:
        options = apply_export_runtime_options({
            "export_3d": args.export_3d,
            "export_svg": args.export_svg,
            "export_bom": args.export_bom,
            "export_sch_pdf": args.export_sch_pdf,
            "export_pos": args.export_pos,
            "export_step": args.export_step,
            "export_gerbers": args.export_gerbers,
            "export_drills": args.export_drills,
            "export_ibom": args.export_ibom,
            "format_jlc": args.format_jlc,
            "version": args.version_tag,
            "sync_title_block_rev": args.sync_title_block_rev,
        })
        
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
