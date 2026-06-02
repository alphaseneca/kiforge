# pyrefly: ignore [missing-import]
import sys
import os
import json
import threading
import time
import logging

# pyrefly: ignore [missing-import]
import wx

# Try importing pcbnew. If it's not available (e.g. running in standard Python shell),
# handle it gracefully for standalone mode.
try:
    # pyrefly: ignore [missing-import]
    import pcbnew
    has_pcbnew = True
except ImportError:
    has_pcbnew = False

# Try importing core exporter logic depending on package context
try:
    from . import kiforge
except ImportError:
    import kiforge

logger = logging.getLogger("KiForge.Studio")

class KiForgeStudioSettingsDialog(wx.Dialog):
    """
    Settings dialog interface for KiForge Studio.
    Provides graphical configuration for project path resolution, output options,
    defaults storage (.kiforge.json), and GitHub Actions CI workflow generation.
    """
    
    def __init__(self, parent, project_dir=None):
        """
        Initializes the settings dialog window.
        
        Args:
            parent: The parent wxWindow or None if running standalone.
            project_dir (str, optional): Pre-resolved project root folder.
        """
        super(KiForgeStudioSettingsDialog, self).__init__(
            parent, 
            title="KiForge Studio - Exporter Settings", 
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.project_dir = project_dir
        self.settings = self.load_settings(project_dir)
        
        self.init_ui()
        self.update_ui_from_settings()
        self.Center()

    def load_settings(self, project_dir):
        """
        Loads configuration settings from the project's .kiforge.json file.
        Falls back to default configurations if the file does not exist or fails to parse.
        
        Args:
            project_dir (str): The project root directory to inspect.
            
        Returns:
            dict: The dictionary of loaded or default configuration options.
        """
        defaults = {
            'output_dir': 'kiforge',
            'export_gerbers': True,
            'export_drills': True,
            'export_pos': True,
            'export_bom': True,
            'export_ibom': True,
            'export_sch_pdf': True,
            'export_step': True,
            'export_3d': True,
            'export_svg': True,
        }
        if not project_dir:
            return defaults
            
        settings_file = os.path.join(project_dir, ".kiforge.json")
        if os.path.isfile(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    for k, v in loaded.items():
                        if k in defaults:
                            if isinstance(defaults[k], bool) and isinstance(v, str):
                                defaults[k] = v.lower() == 'true'
                            else:
                                defaults[k] = v
            except Exception as e:
                logger.warning(f"Failed to load settings from {settings_file}: {e}")
        return defaults

    def init_ui(self):
        """Builds and lays out the wxPython user interface components, panels, and controls."""
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 1. Header Banner
        banner_panel = wx.Panel(self)
        banner_panel.SetBackgroundColour(wx.Colour(30, 41, 59))  # Dark slate blue (#1e293b)
        banner_sizer = wx.BoxSizer(wx.VERTICAL)
        
        lbl_title = wx.StaticText(banner_panel, label="KiForge Studio")
        lbl_title.SetForegroundColour(wx.Colour(255, 255, 255))
        title_font = wx.Font(15, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        lbl_title.SetFont(title_font)
        
        lbl_subtitle = wx.StaticText(banner_panel, label="Automated manufacturing & documentation exports")
        lbl_subtitle.SetForegroundColour(wx.Colour(203, 213, 225)) # Light slate (#cbd5e1)
        subtitle_font = wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        lbl_subtitle.SetFont(subtitle_font)
        
        banner_sizer.Add(lbl_title, 0, wx.ALL | wx.ALIGN_LEFT, 10)
        banner_sizer.Add(lbl_subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.ALIGN_LEFT, 10)
        banner_panel.SetSizer(banner_sizer)
        main_sizer.Add(banner_panel, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        # Content sizer (with margins)
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 2. Project Directory Selector
        dir_box = wx.StaticBox(self, label="KiCad Project Directory")
        dir_sizer = wx.StaticBoxSizer(dir_box, wx.HORIZONTAL)
        self.txt_project_dir = wx.TextCtrl(dir_box, style=wx.TE_LEFT)
        if self.project_dir:
            self.txt_project_dir.SetValue(self.project_dir)
        btn_browse = wx.Button(dir_box, label="Browse...")
        btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        
        dir_sizer.Add(self.txt_project_dir, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        dir_sizer.Add(btn_browse, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        content_sizer.Add(dir_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        # 3. Checkboxes Columns
        chk_container_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        # Column 1: Manufacturing
        mfg_box = wx.StaticBox(self, label="Manufacturing Outputs")
        mfg_sizer = wx.StaticBoxSizer(mfg_box, wx.VERTICAL)
        self.chk_gerbers = wx.CheckBox(mfg_box, label="Gerber Layers (.gbr)")
        self.chk_drills = wx.CheckBox(mfg_box, label="Drill Files (.drl)")
        self.chk_pos = wx.CheckBox(mfg_box, label="Component Placement (CPL)")
        self.chk_bom = wx.CheckBox(mfg_box, label="Bill of Materials (BOM)")
        self.chk_ibom = wx.CheckBox(mfg_box, label="Interactive HTML BOM (iBOM)")
        
        mfg_sizer.Add(self.chk_gerbers, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_drills, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_pos, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_bom, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_ibom, 0, wx.ALL, 6)
        
        # Column 2: Documentation & Models
        doc_box = wx.StaticBox(self, label="Documentation & Models")
        doc_sizer = wx.StaticBoxSizer(doc_box, wx.VERTICAL)
        self.chk_sch_pdf = wx.CheckBox(doc_box, label="Schematic PDF")
        self.chk_step = wx.CheckBox(doc_box, label="STEP 3D Model (.step)")
        self.chk_3d = wx.CheckBox(doc_box, label="3D View Renders (PNG)")
        self.chk_svg = wx.CheckBox(doc_box, label="Copper Layer SVGs")
        
        doc_sizer.Add(self.chk_sch_pdf, 0, wx.ALL, 6)
        doc_sizer.Add(self.chk_step, 0, wx.ALL, 6)
        doc_sizer.Add(self.chk_3d, 0, wx.ALL, 6)
        doc_sizer.Add(self.chk_svg, 0, wx.ALL, 6)
        
        chk_container_sizer.Add(mfg_sizer, 1, wx.EXPAND | wx.RIGHT, 5)
        chk_container_sizer.Add(doc_sizer, 1, wx.EXPAND | wx.LEFT, 5)
        content_sizer.Add(chk_container_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        # 4. Output Folder
        output_box = wx.StaticBox(self, label="Output Configuration")
        output_sizer = wx.StaticBoxSizer(output_box, wx.HORIZONTAL)
        lbl_out = wx.StaticText(output_box, label="Output Directory Name:")
        self.txt_output_dir = wx.TextCtrl(output_box)
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))
        
        output_sizer.Add(lbl_out, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        output_sizer.Add(self.txt_output_dir, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        content_sizer.Add(output_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        # 5. CI/CD Section
        ci_box = wx.StaticBox(self, label="CI/CD Release Integration")
        ci_sizer = wx.StaticBoxSizer(ci_box, wx.VERTICAL)
        
        lbl_ci_desc = wx.StaticText(ci_box, label="Generate GitHub Actions release workflow matching selections.")
        lbl_ci_desc.SetForegroundColour(wx.Colour(100, 116, 139)) # Slate gray (#64748b)
        btn_generate_ci = wx.Button(ci_box, label="Generate CI Files (.github/ & .gitignore)")
        btn_generate_ci.Bind(wx.EVT_BUTTON, self.on_generate_ci)
        
        ci_sizer.Add(lbl_ci_desc, 0, wx.ALL, 5)
        ci_sizer.Add(btn_generate_ci, 0, wx.ALL | wx.EXPAND, 5)
        content_sizer.Add(ci_sizer, 0, wx.EXPAND | wx.BOTTOM, 15)
        
        # 6. Action buttons in Footer
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        btn_save = wx.Button(self, label="Save Defaults")
        btn_save.Bind(wx.EVT_BUTTON, self.on_save_defaults)
        
        btn_export = wx.Button(self, label="Run Export Now")
        btn_export.SetDefault()
        btn_export.Bind(wx.EVT_BUTTON, self.on_run_export)
        
        btn_close = wx.Button(self, wx.ID_CANCEL, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)
        
        footer_sizer.Add(btn_save, 0, wx.RIGHT, 10)
        footer_sizer.AddStretchSpacer()
        footer_sizer.Add(btn_export, 0, wx.RIGHT, 10)
        footer_sizer.Add(btn_close, 0)
        
        content_sizer.Add(footer_sizer, 0, wx.EXPAND)
        
        main_sizer.Add(content_sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)
        self.SetSizerAndFit(main_sizer)

    def update_ui_from_settings(self):
        """Updates the dialog checkboxes and text values to reflect self.settings contents."""
        self.chk_gerbers.SetValue(self.settings.get('export_gerbers', True))
        self.chk_drills.SetValue(self.settings.get('export_drills', True))
        self.chk_pos.SetValue(self.settings.get('export_pos', True))
        self.chk_bom.SetValue(self.settings.get('export_bom', True))
        self.chk_ibom.SetValue(self.settings.get('export_ibom', True))
        self.chk_sch_pdf.SetValue(self.settings.get('export_sch_pdf', True))
        self.chk_step.SetValue(self.settings.get('export_step', True))
        self.chk_3d.SetValue(self.settings.get('export_3d', True))
        self.chk_svg.SetValue(self.settings.get('export_svg', True))
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))

    def on_browse(self, event):
        """Triggered by the 'Browse...' button to select a project root directory."""
        default_dir = self.txt_project_dir.GetValue()
        if not default_dir or not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
            
        dlg = wx.DirDialog(self, "Select KiCad Project Directory", default_dir,
                            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            chosen_dir = dlg.GetPath()
            self.txt_project_dir.SetValue(chosen_dir)
            self.project_dir = chosen_dir
            self.settings = self.load_settings(chosen_dir)
            self.update_ui_from_settings()
        dlg.Destroy()

    def on_save_defaults(self, event):
        """Saves current GUI checkbox and directory selections into a project-local .kiforge.json file."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        settings = {
            'output_dir': self.txt_output_dir.GetValue().strip(),
            'export_gerbers': self.chk_gerbers.IsChecked(),
            'export_drills': self.chk_drills.IsChecked(),
            'export_pos': self.chk_pos.IsChecked(),
            'export_bom': self.chk_bom.IsChecked(),
            'export_ibom': self.chk_ibom.IsChecked(),
            'export_sch_pdf': self.chk_sch_pdf.IsChecked(),
            'export_step': self.chk_step.IsChecked(),
            'export_3d': self.chk_3d.IsChecked(),
            'export_svg': self.chk_svg.IsChecked(),
        }
        
        settings_file = os.path.join(project_dir, ".kiforge.json")
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4)
            wx.MessageBox("Default settings saved successfully to:\n" + settings_file, 
                          "Settings Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            wx.MessageBox(f"Failed to save settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_generate_ci(self, event):
        """Generates the GitHub Actions release workflow YAML and updates .gitignore based on current GUI selections."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            wx.MessageBox("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        options = {
            'export_gerbers': self.chk_gerbers.IsChecked(),
            'export_drills': self.chk_drills.IsChecked(),
            'export_pos': self.chk_pos.IsChecked(),
            'export_bom': self.chk_bom.IsChecked(),
            'export_ibom': self.chk_ibom.IsChecked(),
            'export_sch_pdf': self.chk_sch_pdf.IsChecked(),
            'export_step': self.chk_step.IsChecked(),
            'export_3d': self.chk_3d.IsChecked(),
            'export_svg': self.chk_svg.IsChecked(),
        }
        
        msg, success = kiforge.generate_ci_files(project_dir, output_dir_name, options)
        if success:
            wx.MessageBox(msg, "CI Files Generated", wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)

    def on_run_export(self, event):
        """
        Runs the KiForge export pipeline in a background worker thread.
        Monitors progress on the main thread and displays a modal cancelable ProgressDialog.
        """
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            wx.MessageBox("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR)
            return

        export_flags = {
            'export_gerbers': self.chk_gerbers.IsChecked(),
            'export_drills': self.chk_drills.IsChecked(),
            'export_pos': self.chk_pos.IsChecked(),
            'export_bom': self.chk_bom.IsChecked(),
            'export_ibom': self.chk_ibom.IsChecked(),
            'export_sch_pdf': self.chk_sch_pdf.IsChecked(),
            'export_step': self.chk_step.IsChecked(),
            'export_3d': self.chk_3d.IsChecked(),
            'export_svg': self.chk_svg.IsChecked(),
        }

        # Hide main dialog during export execution
        self.Hide()

        state = {
            'running': True,
            'success': False,
            'error_msg': None,
            'val': 0,
            'msg': "Initializing...",
            'cancelled': False
        }

        def progress_callback(step_index, total_steps, message):
            if step_index is not None and total_steps is not None and total_steps > 0:
                state['val'] = int((step_index / total_steps) * 100)
            if message:
                state['msg'] = message
            return not context.is_aborted()

        # Instantiate and resolve context on main thread
        context = kiforge.ExportContext(project_dir, output_dir_name, export_flags, progress_callback)
        if not context.resolve():
            wx.MessageBox("Failed to resolve project files or KiCad executables.", "KiForge Error", wx.OK | wx.ICON_ERROR)
            self.Show()
            return

        logger.info(f"Resolved project directory: {project_dir}")
        logger.info(f"Resolved output directory: {context.output_dir}")

        progress = wx.ProgressDialog("KiForge", "Initializing exporter...", 100,
                                     style=wx.PD_AUTO_HIDE | wx.PD_APP_MODAL | wx.PD_CAN_ABORT)

        def export_worker():
            try:
                logger.info("Starting background export worker thread...")
                success = kiforge.run_export(context=context)
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

        while state['running']:
            wx.SafeYield()
            keep_going, _ = progress.Update(state['val'], state['msg'])
            if not keep_going:
                state['cancelled'] = True
                logger.warning("Export cancelled by user via progress dialog.")
                context.cancel()
                break
            time.sleep(0.05)

        progress.Hide()
        progress.Destroy()
        wx.SafeYield()

        thread.join(timeout=2.0)

        if state['cancelled']:
            logger.info("Displaying export aborted message box.")
            wx.MessageBox("Export aborted by user.", "KiForge", wx.OK | wx.ICON_WARNING)
        elif state['error_msg']:
            logger.info(f"Displaying error message box: {state['error_msg']}")
            wx.MessageBox(f"An error occurred during export:\n{state['error_msg']}", "KiForge Error", wx.OK | wx.ICON_ERROR)
        elif state['success']:
            logger.info("Displaying export success message box.")
            wx.MessageBox(f"All manufacturing files exported and formatted successfully inside:\n{context.output_dir}", 
                          "KiForge Success", wx.OK | wx.ICON_INFORMATION)
        else:
            logger.info("Displaying unexpected status message box.")
            wx.MessageBox("Export finished with unexpected status.", "KiForge", wx.OK | wx.ICON_WARNING)

        self.EndModal(wx.ID_OK)

    def on_close(self, event):
        """Triggered when the close button is clicked, cancelling dialog and closing."""
        self.EndModal(wx.ID_CANCEL)


# Use ActionPlugin as base when running inside KiCad, plain object otherwise.
# This allows the class to always be defined and importable in standalone/test contexts.
_PluginBase = pcbnew.ActionPlugin if has_pcbnew else object

class ExporterPlugin(_PluginBase):
    """
    ActionPlugin interface implementation that registers KiForge inside the KiCad PCB Editor.
    Extends pcbnew.ActionPlugin when running inside KiCad.
    """
    
    def defaults(self):
        """Sets the default name, category, description, and icon paths for KiCad plugin manager registration."""
        self.name = "KiForge"
        self.category = "Manufacturing"
        self.description = "KiForge Studio - Export Gerbers, Drills, BOM, CPL, STEP, 3D renders, SVGs, and PDFs."
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

    def Run(self):
        """
        Executes the KiForge ActionPlugin when clicked inside the KiCad PCB Editor.
        Attempts to resolve the active project path from the pcbnew Board instance
        and presents the Settings GUI.
        """
        kiforge.setup_logger()
        logger.info("KiForge Studio action plugin invoked.")

        project_dir = None
        try:
            board = pcbnew.GetBoard()
            if board:
                board_file = board.GetFileName()
                if board_file and board_file.endswith(".kicad_pcb"):
                    pro_file = board_file.replace(".kicad_pcb", ".kicad_pro")
                    if os.path.isfile(pro_file):
                        project_dir = os.path.dirname(pro_file)
                    else:
                        temp_dir = os.path.dirname(board_file)
                        pro_files = [f for f in os.listdir(temp_dir) if f.endswith(".kicad_pro")]
                        if pro_files:
                            project_dir = temp_dir
        except Exception as e:
            logger.debug(f"Failed to resolve board filename from pcbnew context: {e}")

        if not project_dir:
            cwd = os.getcwd()
            pro_files = [f for f in os.listdir(cwd) if f.endswith(".kicad_pro")]
            if pro_files:
                project_dir = cwd

        parent_window = None
        app = wx.GetApp()
        if app and hasattr(app, "GetTopWindow"):
            parent_window = app.GetTopWindow()

        dialog = KiForgeStudioSettingsDialog(parent_window, project_dir)
        dialog.ShowModal()
        dialog.Destroy()


# Standalone application execution context
def run_standalone():
    """
    Allows running the KiForge Studio GUI directly as a standalone wx application
    outside the KiCad interface.
    """
    kiforge.setup_logger()
    
    app = wx.App(False)
    
    # Check current directory for project files
    project_dir = os.getcwd()
    pro_files = [f for f in os.listdir(project_dir) if f.endswith(".kicad_pro")]
    if not pro_files:
        project_dir = None
        
    dialog = KiForgeStudioSettingsDialog(None, project_dir)
    dialog.ShowModal()
    dialog.Destroy()


# Direct script run entrypoint
if __name__ == "__main__":
    run_standalone()
