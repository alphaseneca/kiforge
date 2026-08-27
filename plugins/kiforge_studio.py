"""
KiForge Studio — wxPython GUI for KiForge inside KiCad and standalone mode.

Provides the settings dialog (export toggles, placement/STEP params, iBOM options,
config load/save), background export with a themed progress dialog, and CD
workflow generation. Core export logic lives in ``kiforge.py``; this module
handles UI and threading only.

Settings persisted by Studio map to kiforge configuration layers:

- ``exports`` — export checkboxes (Gerbers, BOM, …)
- ``export_params`` — Advanced placement/STEP controls
- ``ibom`` — Interactive HTML BOM presentation checkboxes

BOM column layout and 3D render quality are fixed in ``kiforge.py`` and are not
edited from Studio. See ``ARCHITECTURE.md`` §7.

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


# ---------------------------------------------------------------------------
# Studio palette
# ---------------------------------------------------------------------------
_PAD = 16
_PAD_SM = 10
_COLORS = {
    "app_bg": wx.Colour(24, 24, 27),
    "surface": wx.Colour(39, 39, 42),
    "border": wx.Colour(63, 63, 70),
    "text": wx.Colour(244, 244, 245),
    "muted": wx.Colour(161, 161, 170),
    "footer_bg": wx.Colour(24, 24, 27),
    "input_bg": wx.Colour(33, 33, 38),
    "input_fg": wx.Colour(244, 244, 245),
    "accent": wx.Colour(217, 119, 6),
}

_BUTTON_RADIUS = 6


class _FlatButton(wx.Panel):
    """Flat filled button with rounded corners; paints consistently on Windows and Linux."""

    def __init__(self, parent, label: str, *, primary: bool = False, min_width: int = 0):
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._primary = primary
        self._hover = False
        self._pressed = False
        self._enabled = True
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(parent.GetBackgroundColour() if parent else _COLORS["app_bg"])
        self.SetMinSize((min_width, 32))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

    def _on_enter(self, event):
        if self._enabled:
            self._hover = True
            self.Refresh()
        event.Skip()

    def _on_leave(self, event):
        self._hover = False
        self._pressed = False
        self.Refresh()
        event.Skip()

    def _on_left_down(self, event):
        if not self._enabled:
            return
        self._pressed = True
        self.CaptureMouse()
        self.Refresh()

    def _on_left_up(self, event):
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        self.Refresh()
        if was_pressed and self.ClientRect.Contains(event.GetPosition()):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, self.GetId())
            event.SetEventObject(self)
            wx.PostEvent(self, event)

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetSize()
        parent = self.GetParent()
        parent_bg = parent.GetBackgroundColour() if parent else _COLORS["app_bg"]
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()

        if self._primary:
            accent = _COLORS["accent"]
            if not self._enabled:
                fill = wx.Colour(accent.Red() // 2, accent.Green() // 2, accent.Blue() // 2)
                text = _COLORS["muted"]
                border = fill
            elif self._pressed:
                fill = wx.Colour(
                    max(0, accent.Red() - 24),
                    max(0, accent.Green() - 24),
                    max(0, accent.Blue() - 24),
                )
                text = wx.Colour(255, 255, 255)
                border = fill
            elif self._hover:
                fill = wx.Colour(
                    min(255, accent.Red() + 16),
                    min(255, accent.Green() + 16),
                    min(255, accent.Blue() + 16),
                )
                text = wx.Colour(255, 255, 255)
                border = fill
            else:
                fill = accent
                text = wx.Colour(255, 255, 255)
                border = fill
        else:
            if not self._enabled:
                fill = _COLORS["input_bg"]
                text = _COLORS["muted"]
            elif self._pressed:
                fill = _COLORS["border"]
                text = _COLORS["text"]
            elif self._hover:
                fill = _COLORS["surface"]
                text = _COLORS["text"]
            else:
                fill = _COLORS["input_bg"]
                text = _COLORS["text"]
            border = _COLORS["border"]

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            path = gc.CreatePath()
            path.AddRoundedRectangle(0.5, 0.5, width - 1, height - 1, _BUTTON_RADIUS)
            gc.SetBrush(wx.Brush(fill))
            gc.SetPen(wx.Pen(border, 1))
            gc.DrawPath(path)
        else:
            dc.SetBrush(wx.Brush(fill))
            dc.SetPen(wx.Pen(border, 1))
            dc.DrawRoundedRectangle(0, 0, width, height, _BUTTON_RADIUS)

        dc.SetTextForeground(text)
        font = self.GetFont()
        if self._primary and self._enabled:
            font.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(font)
        tw, th = dc.GetTextExtent(self._label)
        dc.DrawText(self._label, (width - tw) // 2, (height - th) // 2)

    def Enable(self, enable=True):
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        return self.Enable(False)


class _ExportProgressDialog(wx.Dialog):
    """Non-modal export progress window matching Studio theme."""

    def __init__(self, parent):
        super().__init__(
            parent,
            title="KiForge",
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self._cancelled = False
        self.SetBackgroundColour(_COLORS["app_bg"])
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.lbl_message = wx.StaticText(self, label="Initializing exporter…")
        self.lbl_message.SetForegroundColour(_COLORS["text"])
        sizer.Add(self.lbl_message, 0, wx.EXPAND | wx.ALL, _PAD)

        self.gauge = wx.Gauge(self, range=100, size=(-1, 10))
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _PAD)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        self.btn_cancel = _FlatButton(self, "Cancel", min_width=88)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        row.Add(self.btn_cancel, 0, wx.ALL, _PAD_SM)
        sizer.Add(row, 0, wx.EXPAND)

        self.SetSizer(sizer)
        self.SetMinSize((380, 130))
        self.Fit()
        self.CentreOnParent()

    def _on_cancel(self, event):
        if self._cancelled:
            return
        self._cancelled = True
        self.lbl_message.SetLabel("Cancelling…")
        self.btn_cancel.Disable()
        self.Layout()

    def was_cancelled(self) -> bool:
        return self._cancelled

    def update(self, value: int, message: str | None) -> None:
        if message:
            self.lbl_message.SetLabel(message)
        self.gauge.SetValue(max(0, min(100, int(value))))
        self.Layout()


_DIALOG_MIN_WIDTH = 440
_DIALOG_MIN_HEIGHT = 400

EXPORT_PRESET_RADIO_LABELS = (
    "Full",
    "JLCPCB",
    "Documentation",
    "Custom",
)
EXPORT_PRESET_CHOICES = (
    ("full", "Full export"),
    ("jlcpcb", "JLCPCB order"),
    ("documentation", "Documentation"),
    ("custom", "Custom"),
)
EXPORT_PRESETS = {
    "full": {
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
    },
    "jlcpcb": {
        "export_gerbers": True,
        "export_drills": True,
        "export_pos": True,
        "export_bom": True,
        "export_ibom": False,
        "export_sch_pdf": False,
        "export_step": False,
        "export_3d": False,
        "export_svg": False,
        "export_print_pdf": False,
        "format_jlc": True,
    },
    "documentation": {
        "export_gerbers": False,
        "export_drills": False,
        "export_pos": False,
        "export_bom": False,
        "export_ibom": True,
        "export_sch_pdf": True,
        "export_step": True,
        "export_3d": True,
        "export_svg": True,
        "export_print_pdf": True,
        "format_jlc": False,
    },
}
_EXPORT_TOGGLE_KEYS = (
    "export_gerbers", "export_drills", "export_pos", "export_bom", "export_ibom",
    "export_sch_pdf", "export_step", "export_3d", "export_svg", "export_print_pdf",
)

_TAB_ICON_NAMES = ("export", "advanced", "releases")
_TAB_ICON_RASTER_SIZE = 48
_tab_icon_bitmap_cache: dict[tuple[str, int], wx.Bitmap] = {}


def _load_tab_icon_bitmap(name: str, size: int = 20) -> wx.Bitmap | None:
    """Rasterize a cached/CDN Material Symbol for notebook tabs."""
    cache_key = (name, size)
    if cache_key in _tab_icon_bitmap_cache:
        cached_bmp = _tab_icon_bitmap_cache[cache_key]
        return cached_bmp if cached_bmp.IsOk() else None

    svg_data = kiforge.fetch_tab_icon_svg(name)
    if not svg_data:
        _tab_icon_bitmap_cache[cache_key] = wx.Bitmap()
        return None
    try:
        tinted = kiforge.prepare_tab_icon_svg(svg_data)
        bundle = wx.BitmapBundle.FromSVG(tinted, (_TAB_ICON_RASTER_SIZE, _TAB_ICON_RASTER_SIZE))
        bitmap = bundle.GetBitmap(wx.Size(size, size))
        _tab_icon_bitmap_cache[cache_key] = bitmap
        return bitmap if bitmap.IsOk() else None
    except Exception as exc:
        logger.warning("Failed to rasterize tab icon %s: %s", name, exc)
        return None


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


def _destroy_progress_dialog(progress):
    if not progress:
        return
    try:
        progress.Hide()
    except Exception:
        pass
    try:
        progress.Destroy()
    except Exception:
        pass


class KiForgeStudioSettingsDialog(wx.Dialog):
    """
    Main KiForge Studio dialog: project path, export toggles, and actions.

    Settings load via ``kiforge.load_merged_settings`` (global + project).
    ``_current_settings()`` writes ``exports``, ``export_params``, and ``ibom``
    for ``kiforge.save_settings``. CD workflow YAML is regenerated when
    **Sync with export settings** is enabled.

    Export runs on a background thread; the main thread polls progress with
    ``wx.Timer``.
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
            title="KiForge",
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
        self._applying_preset = False
        self._settings_project_dir = project_dir
        
        self.init_ui()
        self.update_ui_from_settings()
        self._fit_dialog_to_screen()
        self.Center()
        self._bind_keyboard_shortcuts()

        self._export_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_export_progress, self._export_timer)
        self.Bind(wx.EVT_CLOSE, self.on_window_close)

    def on_window_close(self, event):
        """Handle title-bar close while an export may still be running."""
        if self._export_running:
            if wx.MessageBox(
                "Export is still running. Cancel export and close?",
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
        self.SetBackgroundColour(_COLORS["app_bg"])
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self._build_header_panel(), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _PAD)
        main_sizer.Add(self._separator(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _PAD)

        self.notebook = wx.Notebook(self, style=wx.BK_DEFAULT)
        self.notebook.SetBackgroundColour(_COLORS["app_bg"])
        try:
            self.notebook.SetForegroundColour(_COLORS["text"])
        except Exception:
            pass
        self._build_export_tab()
        self._build_advanced_tab()
        self._build_releases_tab()
        self._apply_notebook_icons()
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, _PAD)
        main_sizer.Add(self._separator(self), 0, wx.EXPAND)
        main_sizer.Add(self._build_footer_panel(), 0, wx.EXPAND)

        self.SetSizer(main_sizer)
        self.SetMinSize((_DIALOG_MIN_WIDTH, _DIALOG_MIN_HEIGHT))

        self.Bind(wx.EVT_SIZE, self._on_dialog_resize)
        self._cd_sync_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_cd_sync_timer, self._cd_sync_timer)
        self._bind_live_cd_sync_handlers()
        self._prefetch_tab_icons_async()

    def _prefetch_tab_icons_async(self):
        """Warm the icon cache from CDN without blocking dialog construction."""
        def worker():
            for name in _TAB_ICON_NAMES:
                if kiforge.read_cached_tab_icon_svg(name):
                    continue
                kiforge.download_tab_icon_svg(name)
            wx.CallAfter(self._apply_notebook_icons)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_notebook_icons(self):
        """Attach Material Symbols icons to notebook tabs (CDN + local SVG cache)."""
        display_size = 20
        for name in _TAB_ICON_NAMES:
            _tab_icon_bitmap_cache.pop((name, display_size), None)
        bitmaps = [_load_tab_icon_bitmap(name, display_size) for name in _TAB_ICON_NAMES]
        if not all(bmp and bmp.IsOk() for bmp in bitmaps):
            return
        image_list = wx.ImageList(display_size, display_size)
        for bmp in bitmaps:
            image_list.Add(bmp)
        self.notebook.AssignImageList(image_list)
        for index in range(self.notebook.GetPageCount()):
            self.notebook.SetPageImage(index, index)

    def _bind_keyboard_shortcuts(self):
        def on_char_hook(event):
            if event.GetModifiers() == wx.MOD_CONTROL:
                key = event.GetKeyCode()
                tab_keys = {ord("1"): 0, ord("2"): 1, ord("3"): 2}
                if key in tab_keys:
                    self.notebook.SetSelection(tab_keys[key])
                    return
            elif event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                focused = self.FindFocus()
                if focused and isinstance(focused, wx.TextCtrl):
                    event.Skip()
                    return
                if not self._export_running:
                    self.on_run_export(event)
                    return
            event.Skip()
        self.Bind(wx.EVT_CHAR_HOOK, on_char_hook)

    def _separator(self, parent) -> wx.Panel:
        line = wx.Panel(parent, size=(-1, 1))
        line.SetBackgroundColour(_COLORS["border"])
        line.SetMinSize((-1, 1))
        return line

    def _style_panel(self, panel: wx.Panel, *, surface: bool = True) -> None:
        panel.SetBackgroundColour(_COLORS["surface"] if surface else _COLORS["app_bg"])

    def _style_text(self, label: wx.StaticText, *, muted: bool = False) -> wx.StaticText:
        label.SetForegroundColour(_COLORS["muted"] if muted else _COLORS["text"])
        return label

    def _style_input(self, ctrl: wx.TextCtrl) -> wx.TextCtrl:
        ctrl.SetBackgroundColour(_COLORS["input_bg"])
        ctrl.SetForegroundColour(_COLORS["input_fg"])
        try:
            ctrl.SetInsertionPointEnd()
        except Exception:
            pass
        return ctrl

    def _style_choice(self, ctrl: wx.CheckBox | wx.RadioButton) -> None:
        ctrl.SetBackgroundColour(_COLORS["app_bg"])
        ctrl.SetForegroundColour(_COLORS["text"])
        def _on_focus(evt):
            parent = ctrl.GetParent()
            if parent and parent != ctrl:
                wx.CallAfter(parent.SetFocus)
            evt.Skip()
        ctrl.Bind(wx.EVT_SET_FOCUS, _on_focus)

    def _section_label(self, parent, text: str) -> wx.StaticText:
        lbl = wx.StaticText(parent, label=text)
        lbl.SetForegroundColour(_COLORS["muted"])
        font = lbl.GetFont()
        font.SetWeight(wx.FONTWEIGHT_NORMAL)
        lbl.SetFont(font)
        return lbl

    def _muted_label(self, parent, text: str, wrap: int | None = None) -> wx.StaticText:
        lbl = wx.StaticText(parent, label=text)
        self._style_text(lbl, muted=True)
        if wrap:
            lbl.Wrap(wrap)
        return lbl

    def _build_header_panel(self):
        banner = wx.Panel(self)
        self._style_panel(banner, surface=False)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        accent = wx.Panel(banner, size=(3, 24))
        accent.SetBackgroundColour(_COLORS["accent"])
        accent.SetMinSize((3, 24))
        title = wx.StaticText(banner, label="KiForge")
        title.SetForegroundColour(_COLORS["text"])
        font = title.GetFont()
        font.SetPointSize(13)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        sizer.Add(accent, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        sizer.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        banner.SetSizer(sizer)
        return banner

    def _build_export_tab(self):
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        scroll = wx.ScrolledWindow(page, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 10)
        self._style_panel(scroll, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT

        sizer.Add(self._section_label(scroll, "Project"), 0, inset | wx.TOP, _PAD_SM)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_project_dir = wx.TextCtrl(scroll)
        self._style_input(self.txt_project_dir)
        if self.project_dir:
            self.txt_project_dir.SetValue(self.project_dir)
        self.txt_project_dir.Bind(wx.EVT_KILL_FOCUS, self.on_project_dir_changed)
        btn_browse = _FlatButton(scroll, "Browse", min_width=72)
        btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        row.Add(self.txt_project_dir, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _PAD_SM)
        row.Add(btn_browse, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | inset, _PAD_SM)
        sizer.AddSpacer(_PAD_SM)

        sizer.Add(self._section_label(scroll, "Output folder"), 0, inset, 0)
        self.txt_output_dir = wx.TextCtrl(scroll)
        self._style_input(self.txt_output_dir)
        self.txt_output_dir.SetValue(self.settings.get("output_dir", "kiforge"))
        sizer.Add(self.txt_output_dir, 0, wx.EXPAND | inset | wx.TOP, 6)
        sizer.AddSpacer(_PAD)

        sizer.Add(self._section_label(scroll, "Preset"), 0, inset, 0)
        self._preset_radios = []
        for idx, label in enumerate(EXPORT_PRESET_RADIO_LABELS):
            style = wx.RB_GROUP if idx == 0 else 0
            rb = wx.RadioButton(scroll, label=label, style=style)
            self._style_choice(rb)
            rb.Bind(wx.EVT_RADIOBUTTON, self.on_preset_changed)
            self._preset_radios.append(rb)
            sizer.Add(rb, 0, inset | wx.TOP, 4)

        self.lbl_export_summary = wx.StaticText(scroll, label="")
        self._style_text(self.lbl_export_summary, muted=True)
        sizer.Add(self.lbl_export_summary, 0, inset | wx.TOP, _PAD_SM)

        scroll.SetSizer(sizer)
        scroll.FitInside()
        page.SetSizer(wx.BoxSizer(wx.VERTICAL))
        page.GetSizer().Add(scroll, 1, wx.EXPAND)
        self.notebook.AddPage(page, "Export")

    def _build_advanced_tab(self):
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        scroll = wx.ScrolledWindow(page, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 10)
        self._style_panel(scroll, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT

        sizer.Add(self._section_label(scroll, "Outputs"), 0, inset | wx.TOP, _PAD_SM)
        columns = wx.BoxSizer(wx.HORIZONTAL)

        mfg_col = wx.BoxSizer(wx.VERTICAL)
        mfg_col.Add(self._muted_label(scroll, "Manufacturing"), 0, wx.BOTTOM, 6)
        self.chk_gerbers = wx.CheckBox(scroll, label="Gerbers")
        self.chk_drills = wx.CheckBox(scroll, label="Drill files")
        self.chk_pos = wx.CheckBox(scroll, label="Placement")
        self.chk_bom = wx.CheckBox(scroll, label="BOM")
        self.chk_ibom = wx.CheckBox(scroll, label="Interactive BOM")
        self.chk_gerbers.Bind(wx.EVT_CHECKBOX, self.on_gerbers_toggled)
        for chk in (self.chk_drills, self.chk_pos, self.chk_bom, self.chk_ibom):
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        for chk in (self.chk_gerbers, self.chk_drills, self.chk_pos, self.chk_bom, self.chk_ibom):
            self._style_choice(chk)
            mfg_col.Add(chk, 0, wx.TOP, 4)

        mfg_col.AddSpacer(8)
        side_row = wx.BoxSizer(wx.HORIZONTAL)
        side_row.Add(self._muted_label(scroll, "Placement side"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _PAD_SM)
        self.choice_pos_side = wx.Choice(scroll, choices=["Both", "Front", "Back"])
        self.choice_pos_side.SetBackgroundColour(_COLORS["input_bg"])
        self.choice_pos_side.SetForegroundColour(_COLORS["input_fg"])
        side_row.Add(self.choice_pos_side, 1, wx.EXPAND)
        mfg_col.Add(side_row, 0, wx.EXPAND | wx.TOP, 4)
        self.chk_pos_smd_only = wx.CheckBox(scroll, label="SMD only")
        self.chk_pos_exclude_dnp = wx.CheckBox(scroll, label="Exclude DNP")
        for chk in (self.chk_pos_smd_only, self.chk_pos_exclude_dnp):
            self._style_choice(chk)
            mfg_col.Add(chk, 0, wx.TOP, 4)
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)
        self.choice_pos_side.Bind(wx.EVT_CHOICE, self.on_export_setting_changed)

        mfg_col.AddSpacer(8)
        mfg_col.Add(self._muted_label(scroll, "BOM columns"), 0, wx.BOTTOM, 6)
        self.chk_bom_mfr_mpn = wx.CheckBox(scroll, label="Include Manufacturer & MPN")
        self._style_choice(self.chk_bom_mfr_mpn)
        mfg_col.Add(self.chk_bom_mfr_mpn, 0, wx.TOP, 4)
        self.chk_bom_mfr_mpn.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)

        doc_col = wx.BoxSizer(wx.VERTICAL)
        doc_col.Add(self._muted_label(scroll, "Documentation"), 0, wx.BOTTOM, 6)
        self.chk_sch_pdf = wx.CheckBox(scroll, label="Schematic PDF")
        self.chk_step = wx.CheckBox(scroll, label="STEP")
        self.chk_3d = wx.CheckBox(scroll, label="3D renders")
        self.chk_svg = wx.CheckBox(scroll, label="Copper SVG")
        self.chk_print_pdf = wx.CheckBox(scroll, label="Homebrew PDF")
        for chk in (self.chk_sch_pdf, self.chk_step, self.chk_3d, self.chk_svg):
            self._style_choice(chk)
            doc_col.Add(chk, 0, wx.TOP, 4)
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)

        self._style_choice(self.chk_print_pdf)
        doc_col.Add(self.chk_print_pdf, 0, wx.TOP, 4)
        self.chk_print_pdf.Bind(wx.EVT_CHECKBOX, self.on_print_pdf_toggled)

        columns.Add(mfg_col, 1, wx.EXPAND | wx.RIGHT, 12)
        columns.Add(doc_col, 1, wx.EXPAND)
        sizer.Add(columns, 0, wx.EXPAND | inset | wx.TOP, 6)
        sizer.AddSpacer(_PAD)


        scroll.SetSizer(sizer)
        scroll.FitInside()
        page.SetSizer(wx.BoxSizer(wx.VERTICAL))
        page.GetSizer().Add(scroll, 1, wx.EXPAND)
        self.notebook.AddPage(page, "Advanced")

    def _build_releases_tab(self):
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT | wx.TOP

        sizer.Add(self._section_label(page, "Releases"), 0, inset, _PAD_SM)
        btn_generate_cd = _FlatButton(page, "Set up workflows", primary=True, min_width=160)
        btn_generate_cd.Bind(wx.EVT_BUTTON, self.on_generate_cd)
        sizer.Add(btn_generate_cd, 0, inset | wx.TOP, 6)

        self.chk_generate_cd = wx.CheckBox(page, label="Sync with export settings")
        self._style_choice(self.chk_generate_cd)
        sizer.Add(self.chk_generate_cd, 0, inset | wx.TOP, _PAD_SM)

        self.lbl_cd_sync_status = wx.StaticText(page, label="")
        self._style_text(self.lbl_cd_sync_status, muted=True)
        sizer.Add(self.lbl_cd_sync_status, 0, inset | wx.TOP, 6)

        page.SetSizer(sizer)
        self.notebook.AddPage(page, "Releases")

    def _build_footer_panel(self):
        footer = wx.Panel(self)
        footer.SetBackgroundColour(_COLORS["footer_bg"])
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_save = _FlatButton(footer, "Save", min_width=64)
        btn_save.Bind(wx.EVT_BUTTON, self.on_settings_menu)

        self.btn_export = _FlatButton(footer, "Export", primary=True, min_width=72)
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_run_export)

        btn_close = _FlatButton(footer, "Close", min_width=64)
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)

        sizer.Add(btn_save, 0, wx.ALL, _PAD_SM)
        sizer.AddStretchSpacer()
        sizer.Add(self.btn_export, 0, wx.ALL, _PAD_SM)
        sizer.Add(btn_close, 0, wx.ALL, _PAD_SM)
        footer.SetSizer(sizer)
        return footer

    def _refresh_scroll_layout(self):
        self.Layout()

    def on_settings_menu(self, event):
        menu = wx.Menu()
        item_save_project = menu.Append(wx.ID_ANY, "Save for this project")
        item_save_global = menu.Append(wx.ID_ANY, "Save as global default")
        menu.AppendSeparator()
        item_reset = menu.Append(wx.ID_ANY, "Reset")
        self.Bind(wx.EVT_MENU, self.on_save_project_defaults, item_save_project)
        self.Bind(wx.EVT_MENU, self.on_save_global_defaults, item_save_global)
        self.Bind(wx.EVT_MENU, self.on_reset_defaults, item_reset)
        btn = event.GetEventObject()
        if isinstance(btn, wx.Window):
            btn.PopupMenu(menu, wx.Point(0, 0))
        else:
            self.PopupMenu(menu)
        menu.Destroy()

    def _selected_preset_index(self) -> int:
        for idx, rb in enumerate(self._preset_radios):
            if rb.GetValue():
                return idx
        return -1

    def on_preset_changed(self, event):
        """Apply a quick export preset."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        index = self._selected_preset_index()
        if index < 0:
            return
        preset_id = EXPORT_PRESET_CHOICES[index][0]
        if preset_id == "custom":
            self.notebook.SetSelection(1)
            self._refresh_scroll_layout()
            return
        self._apply_export_preset(preset_id)

    def on_export_checkbox_changed(self, event):
        """Manual output toggles switch the preset to Custom."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        if not self._applying_preset:
            self._set_preset_choice("custom")
        self._update_export_summary()
        self.on_export_setting_changed(event)

    def _apply_export_preset(self, preset_id: str):
        preset = EXPORT_PRESETS.get(preset_id)
        if not preset:
            return
        checkbox_map = {
            "export_gerbers": self.chk_gerbers,
            "export_drills": self.chk_drills,
            "export_pos": self.chk_pos,
            "export_bom": self.chk_bom,
            "export_ibom": self.chk_ibom,
            "export_sch_pdf": self.chk_sch_pdf,
            "export_step": self.chk_step,
            "export_3d": self.chk_3d,
            "export_svg": self.chk_svg,
            "export_print_pdf": self.chk_print_pdf,
        }
        self._applying_preset = True
        try:
            for key, value in preset.items():
                if key == "format_jlc":
                    self.settings["format_jlc"] = value
                    self.settings.setdefault("exports", {})["format_jlc"] = value
                elif key in checkbox_map:
                    checkbox_map[key].SetValue(value)
            self._set_preset_choice(preset_id)
            self._sync_drill_checkbox_state()
            self._sync_svg_pdf_checkbox_state()
            self._update_export_summary()
            self._schedule_cd_sync()
        finally:
            self._applying_preset = False

    def _set_preset_choice(self, preset_id: str):
        labels = [pid for pid, _ in EXPORT_PRESET_CHOICES]
        if preset_id in labels:
            idx = labels.index(preset_id)
            if 0 <= idx < len(self._preset_radios):
                self._preset_radios[idx].SetValue(True)

    def _detect_active_preset(self) -> str:
        current = {key: getattr(self, self._export_checkbox_attr(key)).IsChecked() for key in _EXPORT_TOGGLE_KEYS}
        current["format_jlc"] = self._export_setting("format_jlc")
        for preset_id, values in EXPORT_PRESETS.items():
            if all(current.get(key) == value for key, value in values.items()):
                return preset_id
        return "custom"

    @staticmethod
    def _export_checkbox_attr(export_key: str) -> str:
        mapping = {
            "export_gerbers": "chk_gerbers",
            "export_drills": "chk_drills",
            "export_pos": "chk_pos",
            "export_bom": "chk_bom",
            "export_ibom": "chk_ibom",
            "export_sch_pdf": "chk_sch_pdf",
            "export_step": "chk_step",
            "export_3d": "chk_3d",
            "export_svg": "chk_svg",
            "export_print_pdf": "chk_print_pdf",
        }
        return mapping[export_key]

    def _update_export_summary(self):
        enabled = []
        labels = {
            "export_gerbers": "Gerbers",
            "export_drills": "Drills",
            "export_pos": "CPL",
            "export_bom": "BOM",
            "export_ibom": "iBOM",
            "export_sch_pdf": "Schematic PDF",
            "export_step": "STEP",
            "export_3d": "3D renders",
            "export_svg": "SVG",
            "export_print_pdf": "Homebrew PDF",
        }
        for key in _EXPORT_TOGGLE_KEYS:
            if getattr(self, self._export_checkbox_attr(key)).IsChecked():
                enabled.append(labels[key])
        if not enabled:
            summary = "No outputs"
        else:
            summary = ", ".join(enabled)
        if self._export_setting("format_jlc"):
            summary += " · JLC"
        self.lbl_export_summary.SetLabel(summary)

    def _on_dialog_resize(self, event):
        if hasattr(self, "lbl_export_summary"):
            width = max(_DIALOG_MIN_WIDTH, self.GetClientSize().width)
            self.lbl_export_summary.Wrap(max(160, width - (_PAD * 2) - 8))
        if event is not None:
            event.Skip()

    def _fit_dialog_to_screen(self):
        try:
            display_w, display_h = wx.DisplaySize()
        except Exception:
            display_w, display_h = 1024, 768
        width = min(580, max(_DIALOG_MIN_WIDTH, display_w - 80))
        height = min(520, max(_DIALOG_MIN_HEIGHT, display_h - 100))
        self.SetSize((width, height))
        self._on_dialog_resize(None)
        self.Layout()

    def _bind_live_cd_sync_handlers(self):
        """Regenerate CD YAML when export toggles change (debounced)."""
        self.chk_generate_cd.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)
        self.txt_output_dir.Bind(wx.EVT_TEXT, self.on_export_setting_changed)

    def on_export_setting_changed(self, event):
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self.settings["export_params"] = self._collect_export_params()
        self._schedule_cd_sync()

    def _schedule_cd_sync(self):
        if hasattr(self, "_cd_sync_timer"):
            self._cd_sync_timer.Start(500, oneShot=True)

    def on_cd_sync_timer(self, event):
        self._sync_cd_workflows_silent()

    def _sync_cd_workflows_silent(self):
        # Future: skip auto-regeneration once CD files exist (mid-project lifecycle).
        if not self.chk_generate_cd.IsChecked():
            return
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
        self.chk_print_pdf.SetValue(self._export_setting('export_print_pdf'))
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))
        self.chk_generate_cd.SetValue(
            self._export_setting('generate_cd', self.settings.get('generate_ci', True))
        )
        self._set_preset_choice(self._detect_active_preset())
        self._sync_drill_checkbox_state()
        self._sync_svg_pdf_checkbox_state()
        self._update_export_summary()
        self._apply_export_params_to_ui()

    def _export_param(self, key, default=None):
        """Read one placement/STEP value from nested export_params or flat settings."""
        params = self.settings.get("export_params", {})
        if isinstance(params, dict) and key in params:
            return params[key]
        if key in self.settings:
            return self.settings[key]
        return kiforge.DEFAULT_EXPORT_PARAMS.get(key, default)

    def _collect_export_params(self) -> dict:
        """Build export_params from Advanced tab controls for save/export/CD sync."""
        side_map = ("both", "front", "back")
        selection = self.choice_pos_side.GetSelection()
        if selection < 0:
            selection = 0
        saved = self.settings.get("export_params")
        if not isinstance(saved, dict):
            saved = {}
        params = kiforge.merge_export_params(saved, None)
        params.update({
            "pos_side": side_map[min(selection, 2)],
            "pos_smd_only": self.chk_pos_smd_only.IsChecked(),
            "pos_exclude_dnp": self.chk_pos_exclude_dnp.IsChecked(),
            "bom_include_mfr_mpn": self.chk_bom_mfr_mpn.IsChecked(),
        })
        return params

    def _apply_export_params_to_ui(self):
        side_map = {"both": 0, "front": 1, "back": 2}
        self.choice_pos_side.SetSelection(side_map.get(self._export_param("pos_side", "both"), 0))
        self.chk_pos_smd_only.SetValue(bool(self._export_param("pos_smd_only", True)))
        self.chk_pos_exclude_dnp.SetValue(bool(self._export_param("pos_exclude_dnp", True)))
        self.chk_bom_mfr_mpn.SetValue(bool(self._export_param("bom_include_mfr_mpn", True)))

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
            'export_print_pdf': self.chk_print_pdf.IsChecked(),
            'format_jlc': self._export_setting('format_jlc'),
            'generate_cd': self.chk_generate_cd.IsChecked(),
        }
        return {
            'output_dir': self.txt_output_dir.GetValue().strip(),
            **exports,
            'exports': exports,
            'export_params': self._collect_export_params(),
        }

    def _sync_drill_checkbox_state(self):
        """Drill export is required whenever Gerbers are enabled."""
        if self.chk_gerbers.IsChecked():
            self.chk_drills.SetValue(True)
            self.chk_drills.Disable()
        else:
            self.chk_drills.Enable()

    def _sync_svg_pdf_checkbox_state(self):
        """Copper SVG export is required whenever Print PDF is enabled."""
        if self.chk_print_pdf.IsChecked():
            self.chk_svg.SetValue(True)
            self.chk_svg.Disable()
        else:
            self.chk_svg.Enable()

    def on_gerbers_toggled(self, event):
        """Keep drill export aligned with Gerber export requirements."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self._sync_drill_checkbox_state()
        self.on_export_checkbox_changed(event)

    def on_print_pdf_toggled(self, event):
        """Keep Copper SVG export aligned with Print PDF requirements."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self._sync_svg_pdf_checkbox_state()
        self.on_export_checkbox_changed(event)

    def on_project_dir_changed(self, event):
        """Reload project settings when the project folder field loses focus."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        project_dir = self.txt_project_dir.GetValue().strip()
        if (
            project_dir
            and os.path.isdir(project_dir)
            and project_dir != self._settings_project_dir
        ):
            self.project_dir = project_dir
            self._reload_settings(project_dir)
            self._settings_project_dir = project_dir

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
            self._settings_project_dir = chosen_dir
        dlg.Destroy()

    def on_load_global_defaults(self, event):
        """Reload user-wide global settings into the dialog."""
        self._reload_settings(None)
        self._settings_project_dir = self.txt_project_dir.GetValue().strip() or None

    def on_reset_defaults(self, event):
        """Reset dialog controls to built-in KiForge defaults."""
        self.settings = kiforge.DEFAULT_SETTINGS.copy()
        self.settings["exports"] = kiforge.DEFAULT_EXPORT_SETTINGS.copy()
        self.settings["export_params"] = kiforge.DEFAULT_EXPORT_PARAMS.copy()
        self.update_ui_from_settings()
        wx.MessageBox("Dialog reset to built-in defaults.", "Reset", wx.OK | wx.ICON_INFORMATION)

    def _export_options(self):
        """Build export and CD option flags from the current dialog state."""
        options = kiforge.apply_export_params_to_options(self._current_settings())
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
            curr = self._current_settings()
            target = kiforge.save_settings(curr, project_dir=project_dir, scope="project")
            self.settings = curr
            wx.MessageBox(f"Project defaults saved.", "Config Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            wx.MessageBox(f"Failed to save project settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_save_global_defaults(self, event):
        """Save current selections to the user-wide KiForge settings file."""
        try:
            curr = self._current_settings()
            kiforge.save_settings(curr, scope="global")
            self.settings = curr
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
            if state.get("cancelled"):
                return False
            if step_index is not None and total_steps is not None and total_steps > 0:
                state["val"] = int((step_index / total_steps) * 100)
            if message:
                state["msg"] = message
            return not state.get("cancelled")

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

        self._export_progress = _ExportProgressDialog(self)
        self._export_progress.Show()

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

        self._export_timer.Start(75)

    def _stop_export_timer(self):
        if self._export_timer and self._export_timer.IsRunning():
            self._export_timer.Stop()

    def _destroy_export_progress(self):
        self._stop_export_timer()
        progress = self._export_progress
        self._export_progress = None
        _destroy_progress_dialog(progress)

    def _poll_export_progress(self, event):
        state = self._export_state
        context = self._export_context
        progress = self._export_progress
        thread = self._export_thread
        if not state or not context or not thread:
            self._destroy_export_progress()
            return

        if progress and progress.was_cancelled() and not state["cancelled"]:
            state["cancelled"] = True
            context.cancel()
            self._export_join_deadline = min(self._export_join_deadline, time.time() + 20)
            self._destroy_export_progress()
            progress = None

        _pump_ui_events()

        if state["running"]:
            if progress and (
                state["val"] != self._export_poll_val or state["msg"] != self._export_poll_msg
            ):
                progress.update(state["val"], state["msg"])
                self._export_poll_val = state["val"]
                self._export_poll_msg = state["msg"]
            return

        if thread.is_alive():
            if state["cancelled"]:
                context.cancel()
            thread.join(timeout=0)
            if thread.is_alive():
                if time.time() > self._export_join_deadline:
                    logger.warning("Export worker still running after cancel timeout; releasing UI.")
                    self._finish_export_progress()
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
                "Export is still running. Cancel export and close?",
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
