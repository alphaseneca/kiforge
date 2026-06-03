#!/usr/bin/env python3
"""
KiForge — KiCad 10 Manufacturing & Documentation Exporter
==========================================================

Single source of truth for all manufacturing and documentation exports.
Runs kicad-cli subprocesses in a structured pipeline and formats the
raw outputs into JLCPCB-ready files.

Entry Points
------------
CLI (headless / GitHub Actions / Docker):
    python kiforge.py [--project-path PATH] [--output-dir DIR] [--no-export-*]
    python kiforge.py --generate-ci [--project-path PATH] [--output-dir DIR]

Library (KiCad GUI plugin via kiforge_studio.py):
    context = ExportContext(project_path, output_dir_name, options, progress_callback)
    context.resolve()
    run_export(context=context)

Architecture
------------
PathResolver        — Finds kicad-cli and kicad-python executables across platforms.
ExportContext       — Holds all resolved paths, options, subprocess env, cancellation
                      state, and rotation offsets for a single export run.
JLCPCBFormatter     — Stateless helpers to reformat raw KiCad CSV outputs into the
                      exact column layout expected by JLCPCB (BOM + CPL).
ExportTask          — Abstract base for each export step. Subclasses implement
                      is_applicable() and run() only.
ExportRunner        — Drives the ordered pipeline, handles progress callbacks,
                      propagates cancellation, and cleans up on abort/error.
generate_ci_files() — Standalone helper to write a GitHub Actions release workflow
                      and update .gitignore for a downstream KiCad project.
"""

import os
import sys
import csv
import zipfile
import shutil
import subprocess
import logging
import site
import threading
import json

# Ensure the user's local site-packages folder is in sys.path
# This is critical for KiCad's isolated Python environment to recognize --user pip packages.
if hasattr(site, 'getusersitepackages'):
    user_site = site.getusersitepackages()
    if user_site and user_site not in sys.path:
        sys.path.append(user_site)

# Prevent InteractiveHtmlBom from attempting to open graphical displays/dialogs in headless environments
os.environ["INTERACTIVE_HTML_BOM_NO_DISPLAY"] = "1"

# Configure structured logging for KiForge
def setup_logger(output_dir=None):
    """Sets up a structured logger for KiForge that logs to console and optionally a file"""
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

# Get core logger instance
logger = logging.getLogger("KiForge.Core")

# Legacy module-level footprint rotation offsets for JLCPCB assembly (Deprecated).
# Use configuration files or options dict where possible.
ROTATION_OFFSETS = {}

# Legacy active process tracking for cancellation support (Deprecated)
_active_process = None
_abort_requested = False

def reset_abort():
    """Resets the abort request state before a new run (Deprecated)"""
    global _abort_requested, _active_process
    _abort_requested = False
    _active_process = None

def is_abort_requested():
    """Checks if an abort/cancel was requested (Deprecated)"""
    global _abort_requested
    return _abort_requested

def terminate_active_export():
    """Terminates the currently running export subprocess and marks abort requested (Deprecated)"""
    global _active_process, _abort_requested
    _abort_requested = True
    if _active_process:
        try:
            _active_process.terminate()
            # Wait a short time for graceful exit
            _active_process.wait(timeout=0.3)
        except Exception:
            try:
                _active_process.kill()
            except Exception:
                pass


class PathResolver:
    """Utility class to resolve KiCad executables (cli, python)"""
    
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
            return sys.executable
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
                "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-python",
                "/Applications/KiCad/KiCad.app/Contents/MacOS/python3",
                "/Applications/KiCad/KiCad.app/Contents/MacOS/python",
            ]
            for path in paths:
                if os.path.isfile(path):
                    return path

        return sys.executable


# Backward compatibility wrappers for path resolver
def get_kicad_cli_path():
    return PathResolver.get_kicad_cli_path()

def get_kicad_python_path():
    return PathResolver.get_kicad_python_path()


class ExportContext:
    """Encapsulates configuration and resolved runtime paths/variables for a single KiForge run"""
    
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

    def cancel(self):
        """Cancels the current export runner execution, terminating any active subprocess."""
        with self._lock:
            self._aborted = True
            if self.active_process:
                try:
                    self.active_process.terminate()
                    # Wait a short time for graceful exit
                    self.active_process.wait(timeout=0.3)
                except Exception:
                    try:
                        self.active_process.kill()
                    except Exception:
                        pass

    def is_aborted(self) -> bool:
        """Checks if a cancellation request has been made."""
        with self._lock:
            return self._aborted

    def resolve(self) -> bool:
        """Resolves project directories, target files, and environment settings. Returns True if successful."""
        self.kicad_cli = PathResolver.get_kicad_cli_path()
        self.kicad_python = PathResolver.get_kicad_python_path()
        
        # Configure startupinfo to hide console popups on Windows
        if sys.platform == 'win32':
            self.startupinfo = subprocess.STARTUPINFO()
            self.startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            self.startupinfo.wShowWindow = 0  # SW_HIDE
            
        # Set up subprocess environments to include user-site packages and KiCad 3rd-party site packages
        self.env = os.environ.copy()
        python_paths = []
        if hasattr(site, 'getusersitepackages'):
            user_site = site.getusersitepackages()
            if user_site and os.path.exists(user_site):
                python_paths.append(user_site)
                
        # Propagate current sys.path folders that are part of 3rdparty or site-packages (like KiCad PCM/3rdparty dirs)
        for p in sys.path:
            if p and os.path.isdir(p) and ("3rdparty" in p.lower() or "site-packages" in p.lower()):
                if p not in python_paths:
                    python_paths.append(p)
                    
        if python_paths:
            existing_pp = self.env.get("PYTHONPATH", "")
            added_paths = os.pathsep.join(python_paths)
            self.env["PYTHONPATH"] = f"{added_paths}{os.pathsep}{existing_pp}" if existing_pp else added_paths
                    
        if self.kicad_cli and os.path.isabs(self.kicad_cli):
            kicad_bin_dir = os.path.dirname(self.kicad_cli)
            path_env = self.env.get("PATH", "")
            self.env["PATH"] = f"{kicad_bin_dir}{os.pathsep}{path_env}" if path_env else kicad_bin_dir

        # Find project and board files
        for root, dirs, files in os.walk(self.project_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.history']
            for file in files:
                if file.endswith('.kicad_pro') and not self.pcb_name:
                    self.pcb_name = os.path.splitext(file)[0]
                    self.project_dir = root
                elif file.endswith('.kicad_pcb') and not self.pcb_file:
                    self.pcb_file = os.path.join(root, file)

        if not self.pcb_file:
            self.logger.error("No KiCad board (.kicad_pcb) files found.")
            return False

        if not self.pcb_name:
            self.pcb_name = os.path.splitext(os.path.basename(self.pcb_file))[0]
            self.project_dir = os.path.dirname(self.pcb_file)

        # Locate schematic file based on the base project name (before appending version tag)
        sch_name = f"{self.pcb_name}.kicad_sch"
        potential_sch = os.path.join(self.project_dir, sch_name)
        if os.path.isfile(potential_sch):
            self.sch_file = potential_sch
        else:
            for root, dirs, files in os.walk(self.project_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.history']
                for file in files:
                    if file.endswith('.kicad_sch'):
                        self.sch_file = os.path.join(root, file)
                        break
                if self.sch_file:
                    break

        # Resolve version from options or environment variable
        version = self.options.get("version")
        if not version:
            ref_type = os.environ.get("GITHUB_REF_TYPE", "")
            if ref_type == "tag" or not ref_type:
                version = os.environ.get("GITHUB_REF_NAME")
        if not version:
            version = os.environ.get("VERSION")

        # Fallback: Extract version (revision) from schematic or board file title block
        if not version:
            def _extract_rev(file_path):
                if not file_path or not os.path.isfile(file_path):
                    return None
                try:
                    import re
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if "(rev " in line:
                                match = re.search(r'\(rev\s+"?([^")\s]+)"?\)', line)
                                if match:
                                    rev_val = match.group(1).strip()
                                    if rev_val and rev_val.lower() not in ["rev", "revision"]:
                                        return rev_val
                except Exception:
                    pass
                return None

            version = _extract_rev(self.sch_file)
            if not version:
                version = _extract_rev(self.pcb_file)
            
        if version:
            version_str = version.strip()
            if "/" in version_str:
                version_str = version_str.split("/")[-1]
            if version_str:
                # If version starts with a digit (e.g. "1.0.0"), prepend "v" for a clean "vX.X.X" format
                if version_str[0].isdigit():
                    version_str = f"v{version_str}"
                self.pcb_name = f"{self.pcb_name}_{version_str}"

        # Resolve output directories
        if os.path.isabs(self.output_dir_name):
            self.output_dir = self.output_dir_name
        else:
            self.output_dir = os.path.join(self.project_dir, self.output_dir_name)
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.temp_gerber_dir = os.path.join(self.output_dir, "temp_gerbers")
        os.makedirs(self.temp_gerber_dir, exist_ok=True)

        setup_logger(self.output_dir)
        self.logger.info(f"Resolved project: {self.pcb_name} in {self.project_dir}")
        self.logger.info(f"Target Output Directory: {self.output_dir}")
        self.logger.info(f"Resolved KiCad Python: {self.kicad_python}")
        
        # Load settings and rotation offsets from .kiforge.json if it exists
        json_offsets = {}
        settings_file = os.path.join(self.project_dir, ".kiforge.json")
        if os.path.isfile(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    json_offsets = settings.get("rotation_offsets", {})
                    # If it's a string representation of json (unlikely but possible), parse it
                    if isinstance(json_offsets, str):
                        try:
                            json_offsets = json.loads(json_offsets)
                        except Exception:
                            json_offsets = {}
            except Exception as e:
                self.logger.warning(f"Failed to load settings from {settings_file}: {e}")
                
        # Merge rotation offsets (options take precedence over .kiforge.json)
        merged = json_offsets.copy() if isinstance(json_offsets, dict) else {}
        opt_offsets = self.options.get("rotation_offsets", {})
        if isinstance(opt_offsets, dict):
            merged.update(opt_offsets)
        self.rotation_offsets = merged
        
        return True


class JLCPCBFormatter:
    """Encapsulates logic to format BOM and placement files to JLCPCB specification"""
    
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
        
        # Merge module-level ROTATION_OFFSETS (if any) with parameter rotation_offsets
        offsets = ROTATION_OFFSETS.copy()
        if rotation_offsets:
            offsets.update(rotation_offsets)
        
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


class ExportTask:
    """Base class for all export tasks"""
    
    def __init__(self, name: str):
        self.name = name

    def is_applicable(self, context: ExportContext) -> bool:
        """Determines if the task should execute based on configuration context"""
        raise NotImplementedError

    def run(self, context: ExportContext) -> bool:
        """Executes the task's command or logic. Returns True if successful."""
        raise NotImplementedError

    def _run_subprocess(self, cmd: list, context: ExportContext) -> bool:
        """Utility method to execute a command as a tracking subprocess"""
        if context.is_aborted():
            return False
 
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
                    env=context.env,
                    startupinfo=context.startupinfo
                )
            
            stdout, stderr = context.active_process.communicate()
            
            if stdout.strip():
                context.logger.debug(f"Command stdout:\n{stdout.strip()}")
            if stderr.strip():
                context.logger.debug(f"Command stderr:\n{stderr.strip()}")
                
            if context.active_process.returncode != 0:
                raise subprocess.CalledProcessError(context.active_process.returncode, cmd, output=stdout, stderr=stderr)
            return True
        except subprocess.CalledProcessError as e:
            if context.is_aborted():
                return False
            err_msg = f"Command failed: {' '.join(cmd)}\n\nError:\n{e.stderr or e.stdout}"
            context.logger.error(err_msg)
            raise RuntimeError(err_msg)
        except OSError as e:
            if context.is_aborted():
                return False
            exe_name = cmd[0] if cmd else "unknown"
            err_msg = (
                f"Failed to execute command '{' '.join(cmd)}':\n"
                f"Executable '{exe_name}' could not be found or executed.\n"
                f"Please ensure '{exe_name}' is installed and present in your system PATH environment variable.\n"
                f"System Error: {e}"
            )
            context.logger.error(err_msg)
            raise RuntimeError(err_msg)
        finally:
            with context._lock:
                context.active_process = None


class GerberExportTask(ExportTask):
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
    def __init__(self):
        super().__init__("Exporting Drill Files")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_drills", True) and bool(context.pcb_file)

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
    def __init__(self):
        super().__init__("Exporting Schematic PDF")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_sch_pdf", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        output_pdf = os.path.join(context.output_dir, f"{context.pcb_name}_sch.pdf")
        cmd = [
            context.kicad_cli, "sch", "export", "pdf",
            context.sch_file,
            "-o", output_pdf
        ]
        return self._run_subprocess(cmd, context)


class Step3dExportTask(ExportTask):
    def __init__(self):
        super().__init__("Exporting STEP 3D Model")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_step", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        output_step = os.path.join(context.output_dir, f"{context.pcb_name}.step")
        cmd = [
            context.kicad_cli, "pcb", "export", "step",
            "--no-optimize-step",
            "--subst-models",
            "-f",
            "-o", output_step,
            context.pcb_file
        ]
        try:
            return self._run_subprocess(cmd, context)
        except RuntimeError as e:
            err_msg = str(e)
            if "Cannot use VRML models" in err_msg or "non-mesh formats" in err_msg:
                context.logger.warning(
                    f"STEP export completed with warnings (some components only have VRML models and were skipped): {err_msg}"
                )
                return True
            raise



class Render3dExportTask(ExportTask):
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
        if not self._run_subprocess(cmd_front, context):
            return False

        # Render Back
        back_png = os.path.join(context.output_dir, f"{context.pcb_name}_3d_back.png")
        cmd_back = [
            context.kicad_cli, "pcb", "render", context.pcb_file,
            "--output", back_png,
            "--rotate", "0,180,0", "--preset", "2", "--floor", "--perspective",
            "--zoom", "0.8", "--quality", "high", "--width", "1920", "--height", "1080"
        ]
        return self._run_subprocess(cmd_back, context)


class SvgExportTask(ExportTask):
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
        if not self._run_subprocess(cmd_front, context):
            return False

        # Back SVG
        back_svg = os.path.join(context.output_dir, f"{context.pcb_name}_back.svg")
        cmd_back = [
            context.kicad_cli, "pcb", "export", "svg",
            "-l", "B.Cu,Edge.Cuts", "-m", "-n", "--drill-shape-opt", "2",
            "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
            "--output", back_svg,
            "--black-and-white", context.pcb_file
        ]
        return self._run_subprocess(cmd_back, context)


class InteractiveBomTask(ExportTask):
    def __init__(self):
        super().__init__("Exporting Interactive HTML BOM")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_ibom", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
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
            ibom_run_cmd = [
                py_exe, "-c",
                "import wx, sys; wx.DisableAsserts(); from InteractiveHtmlBom import generate_interactive_bom; sys.exit(generate_interactive_bom.main())"
            ]
            context.logger.info("InteractiveHtmlBom successfully verified in python environment.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            context.logger.info("InteractiveHtmlBom not found/working in target Python environment. Attempting to install via pip...")
            if context.progress_callback:
                context.progress_callback(None, None, "Installing InteractiveHtmlBom dependency...")
            
            pip_success = False
            err_output = ""
            
            try:
                # Try 1: Standard --user install
                subprocess.run(
                    [py_exe, "-m", "pip", "install", "--user", "InteractiveHtmlBom"],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=context.env,
                    startupinfo=context.startupinfo
                )
                pip_success = True
            except subprocess.CalledProcessError as e:
                err_output = e.stderr or e.stdout or str(e)
                context.logger.info("Standard pip install failed. Retrying with --break-system-packages...")
                try:
                    # Try 2: Retry with --break-system-packages (required for PEP 668 environments)
                    subprocess.run(
                        [py_exe, "-m", "pip", "install", "--user", "--break-system-packages", "InteractiveHtmlBom"],
                        check=True,
                        capture_output=True,
                        text=True,
                        env=context.env,
                        startupinfo=context.startupinfo
                    )
                    pip_success = True
                except subprocess.CalledProcessError as e2:
                    err_output = e2.stderr or e2.stdout or str(e2)

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
                    ibom_run_cmd = [
                        py_exe, "-c",
                        "import wx, sys; wx.DisableAsserts(); from InteractiveHtmlBom import generate_interactive_bom; sys.exit(generate_interactive_bom.main())"
                    ]
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
            ibom_cmd = ibom_run_cmd + [
                "--no-browser",
                "--dest-dir", context.output_dir,
                context.pcb_file
            ]
            success = self._run_subprocess(ibom_cmd, context)
            if success:
                # Rename default output (ibom.html) to include the versioned board name
                default_ibom = os.path.join(context.output_dir, "ibom.html")
                target_ibom = os.path.join(context.output_dir, f"{context.pcb_name}_ibom.html")
                if os.path.exists(default_ibom):
                    try:
                        shutil.move(default_ibom, target_ibom)
                        context.logger.info(f"Renamed InteractiveHtmlBom output to {os.path.basename(target_ibom)}")
                        
                        # Update the HTML title of the generated page to match the versioned board name
                        with open(target_ibom, 'r', encoding='utf-8') as html_f:
                            html_content = html_f.read()
                        
                        import re
                        new_content = re.sub(
                            r'<title>.*?</title>',
                            f'<title>{context.pcb_name}</title>',
                            html_content,
                            flags=re.IGNORECASE
                        )
                        
                        # Override pcbdata.metadata.title in JavaScript so it updates inside the page header too
                        override_script = (
                            f"\n<script type=\"text/javascript\">\n"
                            f"  if (typeof pcbdata !== 'undefined' && pcbdata && pcbdata.metadata) {{\n"
                            f"    pcbdata.metadata.title = \"{context.pcb_name}\";\n"
                            f"  }}\n"
                            f"</script>\n"
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
            return success
        else:
            context.logger.warning("Interactive HTML BOM (iBOM) is not installed and pip installation failed. Skipping iBOM export.")
            return True


class GerberPackTask(ExportTask):
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
                return False
        else:
            if os.path.exists(context.temp_gerber_dir):
                shutil.rmtree(context.temp_gerber_dir)
        return True


class JlcBomFormatTask(ExportTask):
    def __init__(self):
        super().__init__("Formatting JLCPCB BOM")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_bom", True) and bool(context.sch_file)

    def run(self, context: ExportContext) -> bool:
        raw_bom_path = os.path.join(context.output_dir, "raw_bom.csv")
        jlc_bom_path = os.path.join(context.output_dir, f"{context.pcb_name}_bom_jlc.csv")
        if not os.path.exists(raw_bom_path):
            context.logger.warning(f"Raw BOM file not found at {raw_bom_path}, skipping formatting.")
            return True
        try:
            JLCPCBFormatter.format_bom(raw_bom_path, jlc_bom_path)
            os.remove(raw_bom_path)
        except Exception as e:
            context.logger.error(f"Error formatting BOM: {e}", exc_info=True)
            return False
        return True


class JlcCplFormatTask(ExportTask):
    def __init__(self):
        super().__init__("Formatting JLCPCB CPL")

    def is_applicable(self, context: ExportContext) -> bool:
        return context.options.get("export_pos", True) and bool(context.pcb_file)

    def run(self, context: ExportContext) -> bool:
        raw_pos_path = os.path.join(context.output_dir, "raw_pos.csv")
        jlc_cpl_path = os.path.join(context.output_dir, f"{context.pcb_name}_cpl_jlc.csv")
        if not os.path.exists(raw_pos_path):
            context.logger.warning(f"Raw position file not found at {raw_pos_path}, skipping formatting.")
            return True
        try:
            JLCPCBFormatter.format_cpl(raw_pos_path, jlc_cpl_path, context.rotation_offsets)
            os.remove(raw_pos_path)
        except Exception as e:
            context.logger.error(f"Error formatting CPL: {e}", exc_info=True)
            return False
        return True


class ExportRunner:
    """Orchestrates sequential execution of export tasks and manages cancel actions"""
    
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
        
        # 2. Post processing tasks
        self.tasks.append(GerberPackTask())
        self.tasks.append(JlcBomFormatTask())
        self.tasks.append(JlcCplFormatTask())

    def execute(self) -> bool:
        """Executes all applicable tasks. Returns True if all executed successfully."""
        applicable_tasks = [t for t in self.tasks if t.is_applicable(self.context)]
        total_steps = len(applicable_tasks)
        
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
                if not success:
                    self._cleanup_temp_dirs()
                    return False
            except Exception as e:
                self.context.logger.error(f"Task '{task.name}' failed with exception: {e}", exc_info=True)
                self._cleanup_temp_dirs()
                raise e

        if self.context.progress_callback:
            self.context.progress_callback(total_steps, total_steps, "Completed successfully!")
            
        self.context.logger.info("KiForge Exporter pipeline executed successfully.")
        return True

    def _cleanup_temp_dirs(self):
        """Cleans up temporary workspace directories on error or abort"""
        if os.path.exists(self.context.temp_gerber_dir):
            try:
                shutil.rmtree(self.context.temp_gerber_dir)
            except Exception:
                pass


def generate_ci_files(project_dir: str, output_dir_name: str, options: dict) -> tuple[str, bool]:
    """
    Generates both GitHub Actions and Gitea Actions release workflows and updates .gitignore.
    Returns a tuple of (message, success).
    """
    github_dir = os.path.join(project_dir, ".github", "workflows")
    gitea_dir = os.path.join(project_dir, ".gitea", "workflows")
    try:
        # 1. GitHub Actions Release Workflow
        os.makedirs(github_dir, exist_ok=True)
        github_yaml_path = os.path.join(github_dir, "release.yml")
        
        github_yaml_content = f"""name: Manufacturing Release (GitHub)

on:
  push:
    tags:
      - 'v*'   # Triggers on tags like v0.1.0, v0.2.0, etc.

permissions:
  contents: write   # Required to create GitHub Releases and upload assets

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          output_dir: '{output_dir_name}'
          export_3d: '{'true' if options.get('export_3d', True) else 'false'}'
          export_svg: '{'true' if options.get('export_svg', True) else 'false'}'
          export_bom: '{'true' if options.get('export_bom', True) else 'false'}'
          export_sch_pdf: '{'true' if options.get('export_sch_pdf', True) else 'false'}'
          export_pos: '{'true' if options.get('export_pos', True) else 'false'}'
          export_step: '{'true' if options.get('export_step', True) else 'false'}'
          export_gerbers: '{'true' if options.get('export_gerbers', True) else 'false'}'
          export_drills: '{'true' if options.get('export_drills', True) else 'false'}'
          export_ibom: '{'true' if options.get('export_ibom', True) else 'false'}'

      - name: Create Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          tag_name: ${{{{ github.ref_name }}}}
          generate_release_notes: true
          files: {output_dir_name}/*   # Upload every generated file directly as a release asset
"""
        with open(github_yaml_path, 'w', encoding='utf-8') as f:
            f.write(github_yaml_content)

        # 2. Gitea Actions Release Workflow
        os.makedirs(gitea_dir, exist_ok=True)
        gitea_yaml_path = os.path.join(gitea_dir, "release.yml")
        
        gitea_yaml_content = f"""name: Manufacturing Release (Gitea)

on:
  push:
    tags:
      - 'v*'

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: https://github.com/actions/checkout@v4

      - name: Run KiForge
        uses: https://github.com/alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          output_dir: '{output_dir_name}'
          export_3d: '{'true' if options.get('export_3d', True) else 'false'}'
          export_svg: '{'true' if options.get('export_svg', True) else 'false'}'
          export_bom: '{'true' if options.get('export_bom', True) else 'false'}'
          export_sch_pdf: '{'true' if options.get('export_sch_pdf', True) else 'false'}'
          export_pos: '{'true' if options.get('export_pos', True) else 'false'}'
          export_step: '{'true' if options.get('export_step', True) else 'false'}'
          export_gerbers: '{'true' if options.get('export_gerbers', True) else 'false'}'
          export_drills: '{'true' if options.get('export_drills', True) else 'false'}'
          export_ibom: '{'true' if options.get('export_ibom', True) else 'false'}'

      - name: Create Release and Upload Assets
        uses: https://github.com/softprops/action-gh-release@v2
        with:
          tag_name: ${{{{ github.ref_name }}}}
          files: {output_dir_name}/*
"""
        with open(gitea_yaml_path, 'w', encoding='utf-8') as f:
            f.write(gitea_yaml_content)
            
        # Update .gitignore
        gitignore_path = os.path.join(project_dir, ".gitignore")
        gitignore_updated = False
        
        target_ignores = [
            f"{output_dir_name}/",
            "*.lck",
            "*.tmp",
            "fp-info-cache",
            "_autosave-*",
            "*.bak",
            "*-backups/",
            "*.kicad_pcb-bak",
            "*.kicad_sch-bak",
            ".history/",
        ]
        
        if os.path.exists(gitignore_path):
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = [line.strip() for line in content.splitlines()]
            missing = []
            for item in target_ignores:
                if item not in lines and f"/{item}" not in lines and f"./{item}" not in lines:
                    missing.append(item)
                    
            if missing:
                with open(gitignore_path, 'a', encoding='utf-8') as f:
                    if not content.endswith("\n"):
                        f.write("\n")
                    f.write("\n# KiCad & KiForge patterns added by KiForge\n")
                    for item in missing:
                        f.write(f"{item}\n")
                gitignore_updated = True
        else:
            default_ignores = [
                "# KiForge output directory",
                f"{output_dir_name}/",
                "",
                "# KiCad temporary and lock files",
                "*.lck",
                "*.tmp",
                "fp-info-cache",
                "_autosave-*",
                "",
                "# KiCad backup files",
                "*.bak",
                "*-backups/",
                "*.kicad_pcb-bak",
                "*.kicad_sch-bak",
                "",
                "# KiCad 10 Local History / Auto-backups",
                ".history/",
            ]
            with open(gitignore_path, 'w', encoding='utf-8') as f:
                f.write("\n".join(default_ignores) + "\n")
            gitignore_updated = True
            
        msg = (
            f"CD workflows generated successfully:\n"
            f"  - GitHub: .github/workflows/release.yml\n"
            f"  - Gitea: .gitea/workflows/release.yml"
        )
        if gitignore_updated:
            msg += f"\n\nAnd KiCad & KiForge ignore patterns added/updated in .gitignore."
        else:
            msg += f"\n\nAll KiCad & KiForge patterns were already ignored in .gitignore."
        return msg, True
    except Exception as e:
        return f"Failed to generate CI files: {e}", False


# Main library run_export entrypoint
def run_export(project_path=None, output_dir=None, export_3d=True, export_svg=True, export_bom=True, export_sch_pdf=True, export_pos=True, export_step=True, export_gerbers=True, export_drills=True, export_ibom=True, progress_callback=None, context=None):
    """Facade matching the original procedural interface, invoking the refactored Runner framework."""
    if context is None:
        options = {
            "export_3d": export_3d,
            "export_svg": export_svg,
            "export_bom": export_bom,
            "export_sch_pdf": export_sch_pdf,
            "export_pos": export_pos,
            "export_step": export_step,
            "export_gerbers": export_gerbers,
            "export_drills": export_drills,
            "export_ibom": export_ibom
        }
        
        context = ExportContext(project_path, output_dir, options, progress_callback)
        if not context.resolve():
            return False
            
    runner = ExportRunner(context)
    return runner.execute()


def parse_cli_args(args=None):
    """Parses command-line arguments for the KiForge exporter CLI"""
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
    parser.add_argument("--version-tag", "--version_tag", dest="version_tag", default=None, help="Version tag to append to output filenames")
    parser.add_argument("--generate-ci", action="store_true", help="Generate GitHub Actions release workflow and update .gitignore instead of exporting")
    return parser.parse_args(args)


if __name__ == "__main__":
    setup_logger()
    args = parse_cli_args()
    
    if args.generate_ci:
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
            "version": args.version_tag
        }
        msg, success = generate_ci_files(args.project_path, args.output_dir, options)
        print(msg)
        sys.exit(0 if success else 1)
        
    try:
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
            "version": args.version_tag
        }
        
        context = ExportContext(args.project_path, args.output_dir, options)
        if not context.resolve():
            sys.exit(1)
            
        success = run_export(context=context)
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Export aborted by user (KeyboardInterrupt).")
        print("\n[KiForge] Export aborted by user.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Export failed: {e}")
        print(f"\n[KiForge ERROR] {e}", file=sys.stderr)
        sys.exit(1)
