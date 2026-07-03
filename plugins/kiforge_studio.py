"""
KiForge Studio — wxPython GUI for KiForge inside KiCad and standalone mode.

Provides the settings dialog (export toggles, iBOM defaults, config load/save),
background export with ``wx.ProgressDialog``, and CD workflow generation. Core
export logic lives in ``kiforge.py``; this module handles UI threading only.

Thread model
------------
* Main thread: dialog, progress updates via ``wx.Timer``, cancellation.
* Worker thread: ``kiforge.run_export(context=...)`` — never call wx from here.

Registration
------------
:class:`ExporterPlugin` extends ``pcbnew.ActionPlugin`` when KiCad is running.
``plugins/__init__.py`` registers it only when ``pcbnew`` is already in
``sys.modules`` so CLI/tests do not trigger KiCad plugin hooks.
"""
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


def _pump_ui_events():
    """Keep wx/KiCad responsive while a background export is running."""
    app = wx.GetApp()
    if app:
        app.ProcessPendingEvents()
    try:
        wx.YieldIfNeeded()
    except Exception:
        pass
    try:
        wx.SafeYield()
    except Exception:
        pass


def _update_progress_dialog(progress, value, message):
    """
    Update wx.ProgressDialog across platforms.

    Returns False when the user clicks Cancel (or the dialog was closed).
    """
    if not progress:
        return True
    try:
        value = max(0, min(100, int(value)))
        result = progress.Update(value, message or "")
        _pump_ui_events()
        if isinstance(result, tuple):
            return bool(result[0])
        return bool(result)
    except RuntimeError:
        return True
    except Exception as exc:
        if exc.__class__.__name__ in ("PyAssertionError", "AssertionError"):
            return True
        logger.debug("Progress dialog update failed.", exc_info=True)
        return True


def _destroy_progress_dialog(progress):
    """Close a wx.ProgressDialog safely on Windows, macOS, and Linux/GTK."""
    if not progress:
        return
    try:
        progress.Hide()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            progress.Close()
        except Exception:
            pass
    try:
        progress.Destroy()
    except Exception:
        pass


class KiForgeStudioSettingsDialog(wx.Dialog):
    """
    Main KiForge Studio dialog: project path, export toggles, iBOM options, and actions.

    Settings are loaded via ``kiforge.load_merged_settings`` (global + project).
    Export runs on a background thread; the main thread polls progress with
    ``wx.Timer``. Checkbox changes debounce-sync CD workflow YAML to the project.
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
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX
        )
        self.project_dir = project_dir
        self.settings = kiforge.load_merged_settings(project_dir)
        self._export_timer = None
        self._export_state = None
        self._export_context = None
        self._export_thread = None
        self._export_progress = None
        self._export_project_dir = None
        self._export_join_deadline = 0.0
        self._export_poll_val = -1
        self._export_poll_msg = ""
        self._export_running = False
        
        self.init_ui()
        self.update_ui_from_settings()
        self._fit_dialog_to_screen()
        self.Center()

        self._export_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_export_progress, self._export_timer)
        self.Bind(wx.EVT_CLOSE, self.on_window_close)

    def on_window_close(self, event):
        """Handle title-bar close while an export may still be running."""
        if self._export_running:
            if wx.MessageBox(
                "Export is still running. Cancel export and close KiForge Studio?",
                "KiForge",
                wx.YES_NO | wx.ICON_WARNING,
                parent=self,
            ) != wx.YES:
                event.Veto()
                return
            if self._export_state:
                self._export_state['cancelled'] = True
            if self._export_context:
                self._export_context.cancel()
            self._finish_export_progress()
        event.Skip()

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
        main_sizer.Add(banner_panel, 0, wx.EXPAND)

        scroll = wx.ScrolledWindow(self, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 12)
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 2. Project Directory Selector
        dir_box = wx.StaticBox(scroll, label="KiCad Project Directory")
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
        mfg_box = wx.StaticBox(scroll, label="Manufacturing Outputs")
        mfg_sizer = wx.StaticBoxSizer(mfg_box, wx.VERTICAL)
        self.chk_gerbers = wx.CheckBox(mfg_box, label="Gerber Layers (.gbr)")
        self.chk_drills = wx.CheckBox(mfg_box, label="Drill Files (.drl)")
        self.chk_pos = wx.CheckBox(mfg_box, label="Component Placement (CPL)")
        self.chk_bom = wx.CheckBox(mfg_box, label="Bill of Materials (BOM)")
        self.chk_ibom = wx.CheckBox(mfg_box, label="Interactive HTML BOM (iBOM)")
        self.chk_gerbers.Bind(wx.EVT_CHECKBOX, self.on_gerbers_toggled)
        
        mfg_sizer.Add(self.chk_gerbers, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_drills, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_pos, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_bom, 0, wx.ALL, 6)
        mfg_sizer.Add(self.chk_ibom, 0, wx.ALL, 6)
        
        # Column 2: Documentation & Models
        doc_box = wx.StaticBox(scroll, label="Documentation & Models")
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
        output_box = wx.StaticBox(scroll, label="Output Configuration")
        output_sizer = wx.StaticBoxSizer(output_box, wx.HORIZONTAL)
        lbl_out = wx.StaticText(output_box, label="Output Directory Name:")
        self.txt_output_dir = wx.TextCtrl(output_box)
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))
        
        output_sizer.Add(lbl_out, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        output_sizer.Add(self.txt_output_dir, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        content_sizer.Add(output_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)

        options_box = wx.StaticBox(scroll, label="Export Options")
        options_sizer = wx.StaticBoxSizer(options_box, wx.VERTICAL)
        self.chk_format_jlc = wx.CheckBox(options_box, label="Apply JLCPCB BOM/CPL formatting & rotation offsets")
        self.chk_generate_cd = wx.CheckBox(options_box, label="Generate/update CD workflow files on export")
        options_sizer.Add(self.chk_format_jlc, 0, wx.ALL, 6)
        options_sizer.Add(self.chk_generate_cd, 0, wx.ALL, 6)
        content_sizer.Add(options_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)

        ibom_box = wx.StaticBox(scroll, label="Interactive HTML BOM Defaults")
        ibom_sizer = wx.StaticBoxSizer(ibom_box, wx.VERTICAL)
        self.ibom_checks = {}
        ibom_labels = {
            "include_tracks": "Include copper tracks",
            "include_netlist": "Include netlist",
            "dark_mode": "Dark mode",
            "checkboxes": "Show checkboxes column",
            "show_fabrication": "Show fabrication layer",
            "hide_pads": "Hide pads",
            "highlight_pin1": "Highlight pin 1",
        }
        ibom_row = wx.BoxSizer(wx.HORIZONTAL)
        ibom_left = wx.BoxSizer(wx.VERTICAL)
        ibom_right = wx.BoxSizer(wx.VERTICAL)
        for idx, (key, label) in enumerate(ibom_labels.items()):
            chk = wx.CheckBox(ibom_box, label=label)
            self.ibom_checks[key] = chk
            (ibom_left if idx % 2 == 0 else ibom_right).Add(chk, 0, wx.ALL, 4)
        ibom_row.Add(ibom_left, 1, wx.EXPAND)
        ibom_row.Add(ibom_right, 1, wx.EXPAND)
        ibom_sizer.Add(ibom_row, 0, wx.EXPAND)
        content_sizer.Add(ibom_sizer, 0, wx.EXPAND | wx.BOTTOM, 10)
        
        # 5. CD Section
        cd_box = wx.StaticBox(scroll, label="CD Release Integration")
        cd_sizer = wx.StaticBoxSizer(cd_box, wx.VERTICAL)
        
        lbl_cd_desc = wx.StaticText(cd_box, label="Generate GitHub & Gitea Actions release CD workflows matching selections.")
        lbl_cd_desc.SetForegroundColour(wx.Colour(100, 116, 139)) # Slate gray (#64748b)
        btn_generate_cd = wx.Button(cd_box, label="Generate CD Files Only (.github/.gitea/.gitignore)")
        btn_generate_cd.Bind(wx.EVT_BUTTON, self.on_generate_cd)
        
        cd_sizer.Add(lbl_cd_desc, 0, wx.ALL, 5)
        cd_sizer.Add(btn_generate_cd, 0, wx.ALL | wx.EXPAND, 5)
        self.lbl_cd_sync_status = wx.StaticText(cd_box, label="")
        self.lbl_cd_sync_status.SetForegroundColour(wx.Colour(100, 116, 139))
        cd_sizer.Add(self.lbl_cd_sync_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        content_sizer.Add(cd_sizer, 0, wx.EXPAND)

        scroll.SetSizer(content_sizer)
        scroll.Layout()
        scroll.FitInside()
        main_sizer.Add(scroll, 1, wx.EXPAND | wx.ALL, 10)

        footer_panel = wx.Panel(self)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_save_project = wx.Button(footer_panel, label="Save Project Defaults")
        btn_save_project.Bind(wx.EVT_BUTTON, self.on_save_project_defaults)
        btn_save_global = wx.Button(footer_panel, label="Save Global Defaults")
        btn_save_global.Bind(wx.EVT_BUTTON, self.on_save_global_defaults)
        btn_reset = wx.Button(footer_panel, label="Reset Defaults")
        btn_reset.Bind(wx.EVT_BUTTON, self.on_reset_defaults)

        btn_export = wx.Button(footer_panel, label="Run Export Now")
        btn_export.SetDefault()
        btn_export.Bind(wx.EVT_BUTTON, self.on_run_export)
        self.btn_export = btn_export

        btn_close = wx.Button(footer_panel, wx.ID_CANCEL, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)

        footer_sizer.Add(btn_save_project, 0, wx.RIGHT, 5)
        footer_sizer.Add(btn_save_global, 0, wx.RIGHT, 5)
        footer_sizer.Add(btn_reset, 0, wx.RIGHT, 15)
        footer_sizer.AddStretchSpacer()
        footer_sizer.Add(btn_export, 0, wx.RIGHT, 10)
        footer_sizer.Add(btn_close, 0)
        footer_panel.SetSizer(footer_sizer)
        main_sizer.Add(footer_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.SetSizer(main_sizer)
        self.SetMinSize((560, 420))
        self._scroll_panel = scroll

        self._cd_sync_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_cd_sync_timer, self._cd_sync_timer)
        self._bind_live_cd_sync_handlers()

    def _fit_dialog_to_screen(self):
        """Size the dialog to fit smaller displays while keeping the footer visible."""
        try:
            display_w, display_h = wx.DisplaySize()
        except Exception:
            display_w, display_h = 1024, 768
        width = min(640, max(560, display_w - 80))
        height = min(680, max(420, display_h - 120))
        self.SetSize((width, height))
        if hasattr(self, "_scroll_panel"):
            self._scroll_panel.Layout()
            self._scroll_panel.FitInside()

    def _bind_live_cd_sync_handlers(self):
        """Regenerate CD YAML when export toggles change (debounced)."""
        for ctrl in (
            self.chk_gerbers, self.chk_drills, self.chk_pos, self.chk_bom, self.chk_ibom,
            self.chk_sch_pdf, self.chk_step, self.chk_3d, self.chk_svg,
            self.chk_format_jlc, self.chk_generate_cd,
        ):
            ctrl.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)
        self.txt_output_dir.Bind(wx.EVT_TEXT, self.on_export_setting_changed)
        for chk in self.ibom_checks.values():
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)

    def on_export_setting_changed(self, event):
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self._schedule_cd_sync()

    def _schedule_cd_sync(self):
        if hasattr(self, "_cd_sync_timer"):
            self._cd_sync_timer.Start(500, oneShot=True)

    def on_cd_sync_timer(self, event):
        self._sync_cd_workflows_silent()

    def _sync_cd_workflows_silent(self):
        project_dir = self.txt_project_dir.GetValue().strip()
        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir) or not output_dir_name:
            return
        try:
            _, success = kiforge.generate_cd_files(project_dir, output_dir_name, self._export_options())
            if success:
                self.lbl_cd_sync_status.SetLabel("CD workflow files synced with current selections.")
        except Exception as exc:
            self.lbl_cd_sync_status.SetLabel(f"CD sync failed: {exc}")

    def _export_setting(self, key, default=None):
        """Read an export toggle from flat settings or nested exports dict."""
        if default is None:
            default = kiforge.DEFAULT_EXPORT_SETTINGS.get(key, True)
        if key in self.settings:
            return self.settings[key]
        exports = self.settings.get("exports", {})
        return exports.get(key, default)

    def update_ui_from_settings(self):
        """Updates the dialog checkboxes and text values to reflect self.settings contents."""
        self.chk_gerbers.SetValue(self._export_setting('export_gerbers'))
        self.chk_drills.SetValue(self._export_setting('export_drills'))
        self.chk_pos.SetValue(self._export_setting('export_pos'))
        self.chk_bom.SetValue(self._export_setting('export_bom'))
        self.chk_ibom.SetValue(self._export_setting('export_ibom'))
        self.chk_sch_pdf.SetValue(self._export_setting('export_sch_pdf'))
        self.chk_step.SetValue(self._export_setting('export_step'))
        self.chk_3d.SetValue(self._export_setting('export_3d'))
        self.chk_svg.SetValue(self._export_setting('export_svg'))
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))
        self.chk_format_jlc.SetValue(self._export_setting('format_jlc'))
        self.chk_generate_cd.SetValue(
            self._export_setting('generate_cd', self.settings.get('generate_ci', True))
        )
        ibom_settings = self.settings.get('ibom', kiforge.DEFAULT_IBOM_SETTINGS)
        for key, chk in self.ibom_checks.items():
            chk.SetValue(ibom_settings.get(key, kiforge.DEFAULT_IBOM_SETTINGS.get(key, False)))
        self._sync_drill_checkbox_state()

    def _reload_settings(self, project_dir=None):
        """Reload merged settings into the dialog (global + project when dir is set)."""
        self.settings = kiforge.load_merged_settings(project_dir)
        self.update_ui_from_settings()

    def _current_settings(self):
        """Collect the current dialog state as a settings dictionary."""
        exports = {
            'export_gerbers': self.chk_gerbers.IsChecked(),
            'export_drills': self.chk_drills.IsChecked(),
            'export_pos': self.chk_pos.IsChecked(),
            'export_bom': self.chk_bom.IsChecked(),
            'export_ibom': self.chk_ibom.IsChecked(),
            'export_sch_pdf': self.chk_sch_pdf.IsChecked(),
            'export_step': self.chk_step.IsChecked(),
            'export_3d': self.chk_3d.IsChecked(),
            'export_svg': self.chk_svg.IsChecked(),
            'format_jlc': self.chk_format_jlc.IsChecked(),
            'generate_cd': self.chk_generate_cd.IsChecked(),
        }
        return {
            'output_dir': self.txt_output_dir.GetValue().strip(),
            **exports,
            'exports': exports,
            'ibom': {
                key: chk.IsChecked() for key, chk in self.ibom_checks.items()
            },
        }

    def _sync_drill_checkbox_state(self):
        """Drill export is required whenever Gerbers are enabled."""
        if self.chk_gerbers.IsChecked():
            self.chk_drills.SetValue(True)
            self.chk_drills.Disable()
        else:
            self.chk_drills.Enable()

    def on_gerbers_toggled(self, event):
        """Keep drill export aligned with Gerber export requirements."""
        self._sync_drill_checkbox_state()

    def on_browse(self, event):
        """Triggered by the 'Browse...' button to select a project root directory."""
        default_dir = self.txt_project_dir.GetValue().strip()
        if not default_dir or not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")

        dlg = wx.DirDialog(
            self,
            "Select KiCad Project Directory",
            defaultPath=os.path.normpath(default_dir),
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            chosen_dir = dlg.GetPath()
            self.txt_project_dir.SetValue(chosen_dir)
            self.project_dir = chosen_dir
            self._reload_settings(chosen_dir)
        dlg.Destroy()

    def on_load_project_defaults(self, event):
        """Reload settings from the project .kiforge.json into the dialog."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
        self._reload_settings(project_dir)

    def on_load_global_defaults(self, event):
        """Reload user-wide global settings into the dialog."""
        self._reload_settings(None)

    def on_reset_defaults(self, event):
        """Reset dialog controls to built-in KiForge defaults."""
        self.settings = kiforge.DEFAULT_SETTINGS.copy()
        self.settings["exports"] = kiforge.DEFAULT_EXPORT_SETTINGS.copy()
        self.settings["ibom"] = kiforge.DEFAULT_IBOM_SETTINGS.copy()
        self.update_ui_from_settings()
        wx.MessageBox("Dialog reset to built-in defaults.", "Reset", wx.OK | wx.ICON_INFORMATION)

    def _export_options(self):
        """Build export and CD option flags from the current dialog state."""
        options = self._current_settings()
        if options['export_gerbers']:
            options['export_drills'] = True
        return options

    def on_save_project_defaults(self, event):
        """Save current selections to the project .kiforge.json file."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
        try:
            target = kiforge.save_settings(self._current_settings(), project_dir=project_dir, scope="project")
            wx.MessageBox(f"Project defaults saved.", "Config Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            wx.MessageBox(f"Failed to save project settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_save_global_defaults(self, event):
        """Save current selections to the user-wide KiForge settings file."""
        try:
            kiforge.save_settings(self._current_settings(), scope="global")
            wx.MessageBox("Global defaults saved.", "Config Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            wx.MessageBox(f"Failed to save global settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_generate_cd(self, event):
        """Generate CD workflow YAML and update .gitignore from current selections."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return

        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            wx.MessageBox("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR)
            return

        msg, success = kiforge.generate_cd_files(project_dir, output_dir_name, self._export_options())
        if success:
            wx.MessageBox(msg, "CD Files Generated", wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)

    def on_run_export(self, event):
        """
        Runs the KiForge export pipeline in a background worker thread.
        Progress is polled with wx.Timer so KiCad's UI thread is not blocked.
        """
        if self._export_running:
            wx.MessageBox(
                "An export is already in progress.",
                "KiForge",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
            return

        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            wx.MessageBox("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            wx.MessageBox("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        export_flags = kiforge.apply_export_runtime_options(self._export_options())

        state = {
            'running': True,
            'success': False,
            'error_msg': None,
            'val': 0,
            'msg': "Initializing...",
            'cancelled': False,
        }

        def progress_callback(step_index, total_steps, message):
            if step_index is not None and total_steps is not None and total_steps > 0:
                state['val'] = int((step_index / total_steps) * 100)
            if message:
                state['msg'] = message
            return not context.is_aborted()

        context = kiforge.ExportContext(project_dir, output_dir_name, export_flags, progress_callback)
        if not context.resolve():
            wx.MessageBox(
                "Failed to resolve project files or KiCad executables.",
                "KiForge Error",
                wx.OK | wx.ICON_ERROR,
                parent=self,
            )
            return

        logger.info(f"Resolved project directory: {project_dir}")
        logger.info(f"Resolved output directory: {context.output_dir}")

        self._export_state = state
        self._export_context = context
        self._export_project_dir = project_dir
        self._export_poll_val = -1
        self._export_poll_msg = ""
        self._export_running = True
        self.btn_export.Disable()

        # Parent to this dialog, not KiCad's top-level frame — safer on Windows.
        progress_style = wx.PD_CAN_ABORT | wx.PD_SMOOTH
        if sys.platform.startswith("linux"):
            # GTK progress dialogs behave better without smooth animation.
            progress_style = wx.PD_CAN_ABORT

        self._export_progress = wx.ProgressDialog(
            "KiForge",
            "Initializing exporter...",
            100,
            parent=self,
            style=progress_style,
        )

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

        self._export_thread = threading.Thread(target=export_worker, daemon=True)
        self._export_join_deadline = time.time() + 600
        self._export_thread.start()

        poll_ms = 100 if sys.platform == "win32" else 50
        self._export_timer.Start(poll_ms)

    def _stop_export_timer(self):
        if self._export_timer and self._export_timer.IsRunning():
            self._export_timer.Stop()

    def _destroy_export_progress(self):
        self._stop_export_timer()
        progress = self._export_progress
        self._export_progress = None
        _destroy_progress_dialog(progress)

    def _poll_export_progress(self, event):
        """Timer handler: refresh wx.ProgressDialog without blocking KiCad's event loop."""
        state = self._export_state
        context = self._export_context
        progress = self._export_progress
        thread = self._export_thread
        if not state or not context or not progress or not thread:
            self._destroy_export_progress()
            return

        _pump_ui_events()

        if state['running']:
            if state['val'] != self._export_poll_val or state['msg'] != self._export_poll_msg:
                keep_going = _update_progress_dialog(progress, state['val'], state['msg'])
                self._export_poll_val = state['val']
                self._export_poll_msg = state['msg']
                if not keep_going:
                    state['cancelled'] = True
                    logger.warning("Export cancelled by user via progress dialog.")
                    context.cancel()
                    self._export_join_deadline = min(self._export_join_deadline, time.time() + 30)
                    _update_progress_dialog(progress, state['val'], "Cancelling export...")
            return

        if thread.is_alive():
            if state['cancelled']:
                context.cancel()
            thread.join(timeout=0)
            if thread.is_alive():
                if time.time() > self._export_join_deadline:
                    logger.error("Export worker did not exit after cancellation request.")
                    context.cancel()
                    thread.join(timeout=0.1)
                return

        self._finish_export_progress()

    def _finish_export_progress(self):
        if not self._export_running:
            return
        state = self._export_state
        context = self._export_context
        project_dir = self._export_project_dir
        self._export_running = False
        self._destroy_export_progress()
        self._export_thread = None
        self._export_state = None
        self._export_context = None
        self._export_project_dir = None
        if hasattr(self, "btn_export"):
            self.btn_export.Enable()
        wx.CallAfter(self._finish_export_ui, state, context, project_dir)

    def _finish_export_ui(self, state, context, project_dir):
        """Present the export result after the progress dialog has fully closed."""
        if not self:
            return
        if state and context:
            self._show_export_result(state, context, project_dir)
        if self.IsModal():
            self.EndModal(wx.ID_OK)

    def _show_export_result(self, state, context, project_dir):
        """Show the export result after the progress dialog closes."""
        if state['cancelled']:
            wx.MessageBox(
                "Export cancelled.",
                "KiForge",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
            return

        if state['error_msg']:
            wx.MessageBox(
                f"Export failed:\n\n{state['error_msg']}",
                "KiForge Error",
                wx.OK | wx.ICON_ERROR,
                parent=self,
            )
            return

        if state['success']:
            try:
                kiforge.save_settings(self._current_settings(), project_dir=project_dir, scope="project")
            except Exception as exc:
                logger.warning(f"Could not save project config after export: {exc}")
            if context.warnings:
                wx.MessageBox(
                    "Export completed with warnings:\n\n"
                    + "\n\n".join(f"- {warning}" for warning in context.warnings)
                    + f"\n\nCompleted files are in:\n{context.output_dir}",
                    "KiForge Completed with Warnings",
                    wx.OK | wx.ICON_WARNING,
                    parent=self,
                )
            else:
                wx.MessageBox(
                    f"Export complete.\n\nFiles saved to:\n{context.output_dir}",
                    "KiForge",
                    wx.OK | wx.ICON_INFORMATION,
                    parent=self,
                )
            return

        summary = "\n\n".join(f"- {warning}" for warning in context.warnings) or (
            "No export steps completed successfully."
        )
        wx.MessageBox(
            f"Export failed:\n\n{summary}",
            "KiForge Export Failed",
            wx.OK | wx.ICON_ERROR,
            parent=self,
        )

    def on_close(self, event):
        """Triggered when the close button is clicked."""
        if self._export_running:
            if wx.MessageBox(
                "Export is still running. Cancel export and close KiForge Studio?",
                "KiForge",
                wx.YES_NO | wx.ICON_WARNING,
                parent=self,
            ) != wx.YES:
                return
            if self._export_state:
                self._export_state['cancelled'] = True
            if self._export_context:
                self._export_context.cancel()
            self._finish_export_progress()
            return
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

        dialog = None
        try:
            dialog = KiForgeStudioSettingsDialog(parent_window, project_dir)
            dialog.ShowModal()
        except Exception as exc:
            logger.exception("KiForge Studio dialog failed to open.")
            try:
                wx.MessageBox(
                    f"KiForge Studio could not open:\n\n{exc}",
                    "KiForge Error",
                    wx.OK | wx.ICON_ERROR,
                )
            except Exception:
                pass
        finally:
            if dialog is not None:
                try:
                    dialog.Destroy()
                except Exception:
                    pass


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
