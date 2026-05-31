#!/usr/bin/env python3
"""
KiForge Core Exporter
Single source of truth for manufacturing and documentation exports
Supports CLI execution (headless) and library import (GUI plugin with progress callback)
"""

import os
import sys
import csv
import zipfile
import shutil
import subprocess
import logging
import site

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

# User-customizable footprint rotation offsets for JLCPCB assembly.
# Key: footprint name substring, Value: rotation angle offset in degrees (float).
# Example: "SOT-23": 180.0
ROTATION_OFFSETS = {}

# Active process tracking for cancellation support
_active_process = None
_abort_requested = False

def reset_abort():
    """Resets the abort request state before a new run"""
    global _abort_requested, _active_process
    _abort_requested = False
    _active_process = None

def is_abort_requested():
    """Checks if an abort/cancel was requested"""
    global _abort_requested
    return _abort_requested

def terminate_active_export():
    """Terminates the currently running export subprocess and marks abort requested"""
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

def get_python_executable():
    """Resolves the actual Python interpreter executable path, even if embedded inside KiCad."""
    exe = sys.executable
    base_name = os.path.basename(exe).lower()
    if 'kicad' in base_name or base_name in ['pcbnew', '_pcbnew.pyd', '_pcbnew.exe', 'pythonw.exe']:
        search_dirs = []
        exe_dir = os.path.dirname(exe)
        search_dirs.append(exe_dir)
        if hasattr(sys, 'prefix'):
            search_dirs.append(sys.prefix)
            search_dirs.append(os.path.join(sys.prefix, 'bin'))
            search_dirs.append(os.path.join(sys.prefix, 'Scripts'))
        if hasattr(sys, 'exec_prefix'):
            search_dirs.append(sys.exec_prefix)
            search_dirs.append(os.path.join(sys.exec_prefix, 'bin'))
        if sys.platform == 'win32':
            candidates = ['kicad-python.exe', 'python.exe', 'pythonw.exe']
        else:
            candidates = ['python3', 'python', 'python3.10', 'python3.9', 'python3.11']
        for directory in search_dirs:
            for name in candidates:
                path = os.path.join(directory, name)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path
        if sys.platform == 'darwin':
            contents_dir = os.path.dirname(exe_dir)
            mac_py = os.path.join(contents_dir, "Frameworks", "Python.framework", "Versions", "Current", "bin", "python3")
            if os.path.isfile(mac_py):
                return mac_py
    return exe

def zip_directory(dir_path, zip_path):
    """Zips all files inside a directory to a zip archive (preserving relative paths)"""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(dir_path):
            for file in files:
                file_full_path = os.path.join(root, file)
                arcname = os.path.relpath(file_full_path, dir_path)
                zipf.write(file_full_path, arcname)

def format_jlc_bom(raw_bom_path, output_bom_path):
    """Converts a raw KiCad BOM to the JLCPCB format with LCSC Part Numbers resolved and DNP filtered"""
    if not os.path.exists(raw_bom_path):
        return
        
    with open(raw_bom_path, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    jlc_rows = []
    # Possible alias names for LCSC part numbers
    lcsc_aliases = ['LCSC', 'LCSC Part', 'LCSC Part #', 'JLCPCB Part', 'JLCPCB Part #', 'LCSC_Part']
    
    for row in rows:
        # Check if the component group is marked as DNP (Do Not Populate)
        dnp = row.get('${DNP}', '').strip().lower() or row.get('DNP', '').strip().lower()
        if dnp in ['1', 'dnp', 'true', 'yes']:
            continue # Exclude DNP components from the assembly BOM
            
        designator = row.get('Reference', '').strip() or row.get('Designator', '').strip()
        comment = row.get('Value', '').strip() or row.get('Comment', '').strip()
        footprint = row.get('Footprint', '').strip()
        qty = row.get('${QUANTITY}', '').strip() or row.get('QUANTITY', '').strip() or row.get('Quantity', '').strip() or row.get('Qty', '1').strip()
        
        # Find LCSC Part Number from potential field names
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

def format_jlc_cpl(raw_pos_path, output_cpl_path):
    """Converts a raw KiCad position file to the JLCPCB CPL format and applies rotation offsets"""
    if not os.path.exists(raw_pos_path):
        return
        
    with open(raw_pos_path, 'r', newline='', encoding='utf-8-sig') as f:
        lines = f.readlines()
        
    # Strip any comment lines KiCad might output at the start (lines starting with '#')
    clean_lines = [line for line in lines if not line.strip().startswith('#')]
    
    reader = csv.DictReader(clean_lines)
    rows = list(reader)
    
    jlc_cpl_rows = []
    for row in rows:
        ref = row.get('Ref', '').strip()
        val = row.get('Val', '').strip()
        package = row.get('Package', '').strip()
        pos_x = row.get('PosX', '').strip()
        pos_y = row.get('PosY', '').strip()
        rot_str = row.get('Rot', '').strip()
        side = row.get('Side', '').strip()
        
        # Parse rotation as float
        try:
            rotation = float(rot_str)
        except ValueError:
            rotation = 0.0
            
        # Check if footprint matches any rotation offset rules
        for pattern, offset in ROTATION_OFFSETS.items():
            if pattern.lower() in package.lower() or pattern.lower() in val.lower():
                rotation = (rotation + offset) % 360.0
                break
                
        # Map KiCad Side to JLCPCB Layer (Top/Bottom)
        layer = 'Top'
        if side.lower() in ['bottom', 'back', 'b.cu']:
            layer = 'Bottom'
            
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

def run_export(project_path, output_dir, export_3d, export_svg, export_bom, export_sch_pdf, export_pos, export_step, export_gerbers, export_drills, export_ibom=True, progress_callback=None):
    """Core function to execute exports and post-processing. Can report progress via callback."""
    project_path = os.path.abspath(project_path)
    
    # Setup startupinfo to prevent command prompt popup on Windows
    startupinfo = None
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    
    # 1. Locate files (excluding .history and hidden folders)
    pcb_pro_file = None
    pcb_file = None
    
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.history']
        for file in files:
            if file.endswith('.kicad_pro') and not pcb_pro_file:
                pcb_pro_file = os.path.join(root, file)
            elif file.endswith('.kicad_pcb') and not pcb_file:
                pcb_file = os.path.join(root, file)
                
    if not pcb_pro_file and not pcb_file:
        logger.error("No KiCad project (.kicad_pro) or board (.kicad_pcb) files found.")
        return False
        
    if pcb_pro_file:
        pcb_name = os.path.splitext(os.path.basename(pcb_pro_file))[0]
        project_dir = os.path.dirname(pcb_pro_file)
    else:
        pcb_name = os.path.splitext(os.path.basename(pcb_file))[0]
        project_dir = os.path.dirname(pcb_file)
        
    sch_file = os.path.join(project_dir, f"{pcb_name}.kicad_sch")
    if not os.path.isfile(sch_file):
        sch_file = None
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '.history']
            for file in files:
                if file.endswith('.kicad_sch'):
                    sch_file = os.path.join(root, file)
                    break
            if sch_file:
                break

    # Determine final output directory
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(project_dir, output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # Initialize file logging in the output directory
    setup_logger(output_dir)
    logger.info(f"Initialized KiForge Exporter for project: {pcb_name}")
    logger.info(f"Output directory: {output_dir}")
    logger.debug(f"Platform: {sys.platform}")
    logger.debug(f"Python Executable: {sys.executable}")
    logger.debug(f"Resolved Python Executable: {get_python_executable()}")
    if hasattr(site, 'getusersitepackages'):
        logger.debug(f"User site-packages: {site.getusersitepackages()}")
    logger.debug(f"sys.path: {sys.path}")
    
    # Paths for processing
    temp_gerber_dir = os.path.join(output_dir, "temp_gerbers")
    raw_bom_path = os.path.join(output_dir, "raw_bom.csv")
    raw_pos_path = os.path.join(output_dir, "raw_pos.csv")
    
    os.makedirs(temp_gerber_dir, exist_ok=True)

    commands = []
    
    # 1. Gerber Files Export
    if export_gerbers and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "export", "gerbers",
            "--use-drill-file-origin",
            "-o", temp_gerber_dir,
            pcb_file
        ], "Exporting Gerber Layers"))
        
    # 2. Drill Files Export
    if export_drills and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "export", "drill",
            "--excellon-separate-th",
            "--excellon-units", "mm",
            "--drill-origin", "plot",
            "-o", temp_gerber_dir,
            pcb_file
        ], "Exporting Drill Files"))
        
    # 3. Position File Export
    if export_pos and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "export", "pos",
            "--format", "csv",
            "--exclude-dnp",
            "--use-drill-file-origin",
            "--units", "mm",
            pcb_file,
            "-o", raw_pos_path
        ], "Exporting Position Data"))
        
    # 4. BOM Export
    if export_bom and sch_file:
        commands.append(([
            "kicad-cli", "sch", "export", "bom",
            "--fields", "Reference,Value,Footprint,Description,${QUANTITY},${DNP},LCSC,LCSC Part,LCSC Part #,JLCPCB Part,JLCPCB Part #,ID",
            "--group-by", "Value,Footprint,LCSC,LCSC Part,LCSC Part #,JLCPCB Part,JLCPCB Part #,${DNP},ID",
            "--ref-range-delimiter", "",
            sch_file, "-o", raw_bom_path
        ], "Exporting Bill of Materials"))
        
    # 5. Schematic PDF
    if export_sch_pdf and sch_file:
        commands.append(([
            "kicad-cli", "sch", "export", "pdf",
            sch_file,
            "-o", os.path.join(output_dir, f"{pcb_name}_sch.pdf")
        ], "Exporting Schematic PDF"))
        
    # 6. STEP Export
    if export_step and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "export", "step",
            "--no-optimize-step",
            "--subst-models",
            "-f",
            "-o", os.path.join(output_dir, f"{pcb_name}.step"),
            pcb_file
        ], "Exporting STEP 3D Model"))
        
    # 7. 3D Front Render
    if export_3d and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "render", pcb_file,
            "--output", os.path.join(output_dir, f"{pcb_name}_3d_front.png"),
            "--rotate", "0,0,0", "--preset", "2", "--floor", "--perspective",
            "--zoom", "0.8", "--quality", "high", "--width", "1920", "--height", "1080"
        ], "Rendering 3D Front View"))
        
        # 8. 3D Back Render
        commands.append(([
            "kicad-cli", "pcb", "render", pcb_file,
            "--output", os.path.join(output_dir, f"{pcb_name}_3d_back.png"),
            "--rotate", "0,180,0", "--preset", "2", "--floor", "--perspective",
            "--zoom", "0.8", "--quality", "high", "--width", "1920", "--height", "1080"
        ], "Rendering 3D Back View"))
        
    # 9. SVGs
    if export_svg and pcb_file:
        commands.append(([
            "kicad-cli", "pcb", "export", "svg",
            "-l", "F.Cu,Edge.Cuts", "-n", "--drill-shape-opt", "2",
            "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
            "--output", os.path.join(output_dir, f"{pcb_name}_front.svg"),
            "--black-and-white", pcb_file
        ], "Exporting Front SVG"))
        
        commands.append(([
            "kicad-cli", "pcb", "export", "svg",
            "-l", "B.Cu,Edge.Cuts", "-m", "-n", "--drill-shape-opt", "2",
            "--cl", "Edge.Cuts", "--exclude-drawing-sheet",
            "--output", os.path.join(output_dir, f"{pcb_name}_back.svg"),
            "--black-and-white", pcb_file
        ], "Exporting Back SVG"))

    # 10. Interactive HTML BOM
    if export_ibom and pcb_file:
        ibom_available = False
        ibom_run_cmd = []
        py_exe = get_python_executable()
        
        # 1. Try importing the module first
        try:
            import InteractiveHtmlBom
            ibom_available = True
            ibom_run_cmd = [py_exe, "-m", "InteractiveHtmlBom.generate_interactive_bom"]
            logger.info("InteractiveHtmlBom successfully imported from sys.path.")
        except ImportError as e:
            logger.debug("InteractiveHtmlBom import failed", exc_info=True)
            # 2. Try installing it via pip inside the current python environment
            logger.info(f"InteractiveHtmlBom not found in environment (Error: {e}). Attempting to install via pip...")
            if progress_callback:
                progress_callback(len(commands), len(commands) + 4, "Installing InteractiveHtmlBom dependency...")
            try:
                # Use --user to avoid write permission issues
                subprocess.run(
                    [py_exe, "-m", "pip", "install", "--user", "InteractiveHtmlBom"],
                    check=True,
                    capture_output=True,
                    text=True,
                    startupinfo=startupinfo
                )
                ibom_available = True
                ibom_run_cmd = [py_exe, "-m", "InteractiveHtmlBom.generate_interactive_bom"]
                logger.info("InteractiveHtmlBom successfully installed via pip.")
            except Exception as e:
                logger.warning(f"Failed to install InteractiveHtmlBom via pip: {e}")
                
        # 3. Fallback: check if the executable command exists in PATH (e.g. system pip path)
        if not ibom_available:
            import shutil
            if shutil.which("generate_interactive_bom"):
                ibom_available = True
                ibom_run_cmd = ["generate_interactive_bom"]

        if ibom_available:
            ibom_cmd = ibom_run_cmd + [
                "--no-browser",
                "--dest-dir", output_dir,
                pcb_file
            ]
            commands.append((ibom_cmd, "Exporting Interactive HTML BOM"))
        else:
            logger.warning("Interactive HTML BOM (iBOM) is not installed and pip installation failed. Skipping iBOM export.")

    total_steps = len(commands) + 3 # +3 for post-processing steps

    # Run subprocess commands
    for idx, (cmd, desc) in enumerate(commands):
        if _abort_requested:
            if os.path.exists(temp_gerber_dir):
                shutil.rmtree(temp_gerber_dir)
            return False

        if progress_callback:
            keep_going = progress_callback(idx, total_steps, f"Running: {desc}...")
            if not keep_going:
                terminate_active_export()
                if os.path.exists(temp_gerber_dir):
                    shutil.rmtree(temp_gerber_dir)
                return False
                
        logger.info(f"[{idx+1}/{total_steps}] Running command: {' '.join(cmd)}")
        try:
            # Run within project_dir for correct internal library/footprint resolution
            global _active_process
            _active_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=project_dir, startupinfo=startupinfo)
            stdout, stderr = _active_process.communicate()
            
            if stdout.strip():
                logger.debug(f"Command stdout:\n{stdout.strip()}")
            if stderr.strip():
                logger.debug(f"Command stderr:\n{stderr.strip()}")
                
            if _active_process.returncode != 0:
                raise subprocess.CalledProcessError(_active_process.returncode, cmd, output=stdout, stderr=stderr)
        except subprocess.CalledProcessError as e:
            if _abort_requested:
                if os.path.exists(temp_gerber_dir):
                    shutil.rmtree(temp_gerber_dir)
                return False
            err_msg = f"Command failed: {' '.join(cmd)}\n\nError:\n{e.stderr or e.stdout}"
            logger.error(err_msg)
            if progress_callback:
                raise RuntimeError(err_msg)
            else:
                sys.exit(1)
        except OSError as e:
            if _abort_requested:
                if os.path.exists(temp_gerber_dir):
                    shutil.rmtree(temp_gerber_dir)
                return False
            exe_name = cmd[0] if cmd else "unknown"
            err_msg = (
                f"Failed to execute command '{' '.join(cmd)}':\n"
                f"Executable '{exe_name}' could not be found or executed.\n"
                f"Please ensure '{exe_name}' is installed and present in your system PATH environment variable.\n"
                f"System Error: {e}"
            )
            logger.error(err_msg)
            if progress_callback:
                raise RuntimeError(err_msg)
            else:
                sys.exit(1)
        finally:
            _active_process = None

    # Post Step 1: Zip Gerbers
    if _abort_requested:
        if os.path.exists(temp_gerber_dir):
            shutil.rmtree(temp_gerber_dir)
        return False
    current_idx = len(commands)
    desc_step = "Zipping Gerber and Drill files"
    if progress_callback:
        progress_callback(current_idx, total_steps, f"Post-Process: {desc_step}...")
    logger.info(f"[{current_idx+1}/{total_steps}] {desc_step}...")
    
    if os.path.exists(temp_gerber_dir) and os.listdir(temp_gerber_dir):
        gerber_zip_path = os.path.join(output_dir, f"{pcb_name}_gerbers.zip")
        try:
            zip_directory(temp_gerber_dir, gerber_zip_path)
            shutil.rmtree(temp_gerber_dir)
        except Exception as e:
            logger.error(f"Error packaging Gerbers: {e}", exc_info=True)
    elif os.path.exists(temp_gerber_dir):
        shutil.rmtree(temp_gerber_dir)
        
    # Post Step 2: Format JLCPCB BOM
    if _abort_requested:
        return False
    current_idx += 1
    desc_step = "Formatting JLCPCB BOM"
    if progress_callback:
        progress_callback(current_idx, total_steps, f"Post-Process: {desc_step}...")
    logger.info(f"[{current_idx+1}/{total_steps}] {desc_step}...")
    
    if os.path.exists(raw_bom_path):
        jlc_bom_path = os.path.join(output_dir, f"{pcb_name}_bom_jlc.csv")
        try:
            format_jlc_bom(raw_bom_path, jlc_bom_path)
            os.remove(raw_bom_path)
        except Exception as e:
            logger.error(f"Error formatting BOM: {e}", exc_info=True)
            
    # Post Step 3: Format JLCPCB CPL
    if _abort_requested:
        return False
    current_idx += 1
    desc_step = "Formatting JLCPCB CPL"
    if progress_callback:
        progress_callback(current_idx, total_steps, f"Post-Process: {desc_step}...")
    logger.info(f"[{current_idx+1}/{total_steps}] {desc_step}...")
    
    if os.path.exists(raw_pos_path):
        jlc_cpl_path = os.path.join(output_dir, f"{pcb_name}_cpl_jlc.csv")
        try:
            format_jlc_cpl(raw_pos_path, jlc_cpl_path)
            os.remove(raw_pos_path)
        except Exception as e:
            logger.error(f"Error formatting CPL: {e}", exc_info=True)

    if progress_callback:
        progress_callback(total_steps, total_steps, "Completed successfully!")
        
    logger.info("KiForge Exporter completed successfully.")
    return True


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
    return parser.parse_args(args)


if __name__ == "__main__":
    args = parse_cli_args()
    
    success = run_export(
        project_path=args.project_path,
        output_dir=args.output_dir,
        export_3d=args.export_3d,
        export_svg=args.export_svg,
        export_bom=args.export_bom,
        export_sch_pdf=args.export_sch_pdf,
        export_pos=args.export_pos,
        export_step=args.export_step,
        export_gerbers=args.export_gerbers,
        export_drills=args.export_drills,
        export_ibom=args.export_ibom
    )
    if not success:
        sys.exit(1)
