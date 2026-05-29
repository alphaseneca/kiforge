# pyrefly: ignore [missing-import]
import sys

# Direct execution guard to prevent ModuleNotFoundError when run directly
if __name__ == "__main__":
    print("KiForge Studio plugin is designed to run within KiCad's internal Python environment.")
    print("It cannot be run directly as a standalone script.")
    sys.exit(1)

# pyrefly: ignore [missing-import]
import pcbnew
import os
# pyrefly: ignore [missing-import]
import wx
import threading
import time
import logging
from . import kiforge

logger = logging.getLogger("KiForge.Studio")

class ExporterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiForge"
        self.category = "Manufacturing"
        self.description = "KiForge Studio - Export Gerbers, Drills, BOM, CPL, STEP, 3D renders, SVGs, and PDFs."
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

    def Run(self):
        # Initialize default console logging first
        kiforge.setup_logger()
        logger.info("KiForge Studio action plugin invoked.")

        # Get the active board layout
        board = pcbnew.GetBoard()
        if not board:
            logger.error("No board loaded. KiForge requires an open PCB layout file.")
            wx.MessageBox("No board loaded. Please open a PCB layout file first.", "Error", wx.OK | wx.ICON_ERROR)
            return

        board_file = board.GetFileName()
        if not board_file or not board_file.endswith(".kicad_pcb"):
            logger.error("PCB file is not saved or path is unresolved.")
            wx.MessageBox("Please save/open the PCB file first to resolve its path.", "Error", wx.OK | wx.ICON_ERROR)
            return

        # Attempt to find the KiCad project file (.kicad_pro)
        pro_file = board_file.replace(".kicad_pcb", ".kicad_pro")
        if not os.path.isfile(pro_file):
            # Fallback: search for any .kicad_pro in the same directory
            project_dir = os.path.dirname(board_file)
            pro_files = [f for f in os.listdir(project_dir) if f.endswith(".kicad_pro")]
            if pro_files:
                pro_file = os.path.join(project_dir, pro_files[0])
            else:
                logger.error(f"Could not locate KiCad project file (.kicad_pro) in directory: {project_dir}")
                wx.MessageBox("Could not locate a KiCad project file (.kicad_pro) in the directory.", "Error", wx.OK | wx.ICON_ERROR)
                return
        else:
            project_dir = os.path.dirname(pro_file)

        # Build output directory path
        output_dir = os.path.join(project_dir, "kiforge")

        # Initialize file logging in the output directory
        kiforge.setup_logger(output_dir)
        logger.info(f"Resolved project directory: {project_dir}")
        logger.info(f"Resolved output directory: {output_dir}")

        # Initialize progress dialog
        progress = wx.ProgressDialog("KiForge", "Initializing exporter...", 100,
                                     style=wx.PD_AUTO_HIDE | wx.PD_APP_MODAL | wx.PD_CAN_ABORT)

        # Shared state for thread communication
        state = {
            'running': True,
            'success': False,
            'error_msg': None,
            'val': 0,
            'msg': "Initializing...",
            'cancelled': False
        }

        # Define progress callback executed by the background thread
        def progress_callback(step_index, total_steps, message):
            state['val'] = int((step_index / total_steps) * 100)
            state['msg'] = message
            return not kiforge.is_abort_requested()

        # Run background export thread
        def export_worker():
            try:
                kiforge.reset_abort()
                logger.info("Starting background export worker thread...")
                success = kiforge.run_export(
                    project_path=project_dir,
                    output_dir="kiforge",
                    export_3d=True,
                    export_svg=True,
                    export_bom=True,
                    export_sch_pdf=True,
                    export_pos=True,
                    export_step=True,
                    export_gerbers=True,
                    export_drills=True,
                    export_ibom=True,
                    progress_callback=progress_callback
                )
                state['success'] = success
                logger.info(f"Background export worker thread finished. Success status: {success}")
            except Exception as e:
                state['success'] = False
                state['error_msg'] = str(e)
                logger.exception("Exception occurred in background export worker thread.")
            finally:
                state['running'] = False

        thread = threading.Thread(target=export_worker)
        thread.daemon = True
        thread.start()

        # Monitor loop on main thread (updates dialog, checks cancel)
        while state['running']:
            # SafeYield allows processing the user interface events (such as the Cancel button)
            wx.SafeYield()
            
            # Update the progress bar dialog.
            # Update() returns (keep_going, skip). If the user clicked Cancel, keep_going is False.
            keep_going, _ = progress.Update(state['val'], state['msg'])
            if not keep_going:
                state['cancelled'] = True
                logger.warning("Export cancelled by user via progress dialog.")
                kiforge.terminate_active_export()
                break
                
            time.sleep(0.05)

        # Ensure the progress dialog is immediately hidden and destroyed,
        # then yield event loop to force UI updates before showing a modal MessageBox.
        progress.Hide()
        progress.Destroy()
        wx.SafeYield()

        # Wait for the thread to fully exit
        thread.join(timeout=2.0)

        # Handle execution results
        if state['cancelled']:
            logger.info("Displaying export aborted message box.")
            wx.MessageBox("Export aborted by user.", "KiForge", wx.OK | wx.ICON_WARNING)
        elif state['error_msg']:
            logger.info(f"Displaying error message box: {state['error_msg']}")
            wx.MessageBox(f"An error occurred during export:\n{state['error_msg']}", "KiForge Error", wx.OK | wx.ICON_ERROR)
        elif state['success']:
            logger.info("Displaying export success message box.")
            wx.MessageBox(f"All manufacturing files exported and formatted successfully inside:\n{output_dir}", 
                          "KiForge Success", wx.OK | wx.ICON_INFORMATION)
        else:
            logger.info("Displaying unexpected status message box.")
            wx.MessageBox("Export finished with unexpected status.", "KiForge", wx.OK | wx.ICON_WARNING)


# Register the ActionPlugin inside KiCad ONLY if a GUI application context is active.
# This prevents C++ assertions (PgmOrNull failed in register_action) in standalone/CLI environments like kicad-cli.
if wx.GetApp() is not None:
    ExporterPlugin().register()
