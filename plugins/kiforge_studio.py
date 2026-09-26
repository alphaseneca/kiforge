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
# KiCad 10 bundles Python 3.9 (macOS ships 3.9.13 inside KiCad.app); keep the
# ``X | None`` annotations below unevaluated so importing this module inside
# KiCad cannot raise TypeError. See ``kiforge.py`` for the same guard.
from __future__ import annotations

# pyrefly: ignore [missing-import]
import re
import os
import sys
import json
import threading
import time
import logging

# pyrefly: ignore [missing-import]
import wx

# pcbnew is only available inside KiCad; absent in standalone and test runs.
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
# 4pt spacing scale. Every margin, gap, inset and spacer in this file comes
# from one of these steps rather than a literal, so horizontal and vertical
# rhythm stay consistent across tabs and dialogs and a change to the scale
# moves everything together. Steps are used semantically, not by size:
#   XS  tight stacking inside a group (checkbox rows)
#   SM  a label against the group it introduces; grouped-control gaps
#   MD  between sibling columns
#   LG  container margin -- the standard inset for a dialog or tab body
#   XL  separating a block from a different kind of block (message vs actions)
#   XXL major/decorative sizing (banner rule height, message glyph)
# Corner radii are a shape property, not spacing, so they keep their own
# small values (_BUTTON_RADIUS, _CHECKBOX_GLYPH_RADIUS) off this scale.
_SP_XS = 4
_SP_SM = 8
_SP_MD = 12
_SP_LG = 16
_SP_XL = 20
_SP_XXL = 24

# Control sizing. Separate from the spacing scale above (these are sizes, not
# gaps) but deliberately kept on the same 4pt grid.
_CTRL_H = 28


def _snap_to_grid(value: int, step: int = _SP_XS) -> int:
    """Round a pixel dimension to the nearest step of the 4pt grid."""
    return max(step, int(round(value / step)) * step)
# Palette -------------------------------------------------------------------
# Two mirrored Zinc ramps (dark / light) selected from the OS appearance at dialog open.
# Both ramps must define exactly the same keys; a missing key raises inside a paint handler.
_DARK_PALETTE = {
    "app_bg": wx.Colour(24, 24, 27),
    "surface": wx.Colour(39, 39, 42),
    "border": wx.Colour(63, 63, 70),
    "text": wx.Colour(244, 244, 245),
    "muted": wx.Colour(161, 161, 170),
    "footer_bg": wx.Colour(24, 24, 27),
    "input_bg": wx.Colour(33, 33, 38),
    "input_fg": wx.Colour(244, 244, 245),
    "accent": wx.Colour(217, 119, 6),
    # Success tone for a completed export, so the confirming button is not
    # the same amber as "Export".
    "success": wx.Colour(34, 197, 94),
}
_LIGHT_PALETTE = {
    "app_bg": wx.Colour(250, 250, 250),
    "surface": wx.Colour(255, 255, 255),
    "border": wx.Colour(212, 212, 216),
    "text": wx.Colour(24, 24, 27),
    "muted": wx.Colour(113, 113, 122),
    "footer_bg": wx.Colour(250, 250, 250),
    "input_bg": wx.Colour(255, 255, 255),
    "input_fg": wx.Colour(24, 24, 27),
    # Amber reads on both grounds, so the brand accent does not flip.
    "accent": wx.Colour(217, 119, 6),
    # Success tone for a completed export, so the confirming button is not
    # the same amber as "Export".
    "success": wx.Colour(22, 163, 74),
}

# Mutated in place by refresh_palette() so all paint-handler lookups pick up the change without call-site edits.
_COLORS = dict(_DARK_PALETTE)

# Tab glyphs are tinted to one flat colour, so the dark ramp's near-white tint
# is invisible on a light ground and vice versa.
_TAB_ICON_TINTS = {"dark": "#e4e4e7", "light": "#3f3f46"}

# Severity colours are saturated enough to survive both grounds, except the
# neutral and info tones, which need a darker step on white.
_MSG_ICON_COLORS_BY_MODE = {
    "dark": {
        "success": "#22c55e",
        "error": "#ef4444",
        "warning": "#d97706",
        "cancelled": "#a1a1aa",
        "info": "#38bdf8",
        "question": "#a1a1aa",
    },
    "light": {
        "success": "#16a34a",
        "error": "#dc2626",
        "warning": "#b45309",
        "cancelled": "#71717a",
        "info": "#0284c7",
        "question": "#71717a",
    },
}

# Mutated in place by refresh_palette() alongside _COLORS; initialised here to keep the palette block self-contained.
_MSG_ICON_COLORS = dict(_MSG_ICON_COLORS_BY_MODE["dark"])

_palette_mode = "dark"


def _detect_kicad_theme() -> str | None:
    """
    Query KiCad's user configuration for active appearance theme (app_theme).
    Returns 'dark', 'light', or None if set to follow system / unconfigured.
    Supports Linux (XDG, ~/.config, Flatpak, Snap), Windows (%APPDATA%), and macOS.
    """
    paths = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            paths.append(os.path.join(appdata, "kicad"))
    elif sys.platform == "darwin":
        paths.append(os.path.expanduser("~/Library/Preferences/kicad"))
    elif sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
        # Linux (native, Flatpak, Snap)
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg_config:
            paths.append(os.path.join(xdg_config, "kicad"))
        paths.append(os.path.expanduser("~/.config/kicad"))
        paths.append(os.path.expanduser("~/.var/app/org.kicad.KiCad/config/kicad"))  # Flatpak
        paths.append(os.path.expanduser("~/snap/kicad/current/.config/kicad"))      # Snap
    else:
        # Generic Unix fallback
        paths.append(os.path.expanduser("~/.config/kicad"))

    for base in paths:
        if not os.path.isdir(base):
            continue
        candidates = []
        try:
            for entry in os.listdir(base):
                sub = os.path.join(base, entry)
                if os.path.isdir(sub):
                    candidates.append((entry, os.path.join(sub, "kicad_common.json")))
        except Exception:
            pass
        candidates.sort(reverse=True)
        candidates.append(("", os.path.join(base, "kicad_common.json")))

        for _ver, cf in candidates:
            if os.path.isfile(cf):
                try:
                    with open(cf, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    theme = data.get("appearance", {}).get("app_theme", 0)
                    if theme == 2:
                        return "dark"
                    elif theme == 1:
                        return "light"
                except Exception:
                    pass
    return None


def _detect_linux_system_dark() -> bool | None:
    """
    Check standard FreeDesktop / GNOME / KDE desktop theme preference on Linux.
    Returns True for dark, False for light, or None if undetermined.
    """
    if not (sys.platform.startswith("linux") or sys.platform.startswith("freebsd")):
        return None
    try:
        import subprocess
        # 1. FreeDesktop / GNOME standard color-scheme preference
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=1
        )
        if res.returncode == 0:
            val = res.stdout.strip().strip("'\"").lower()
            if "dark" in val:
                return True
            elif "default" in val or "light" in val:
                return False

        # 2. GTK theme name fallback (e.g. Adwaita-dark, Yaru-dark)
        res2 = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
            capture_output=True, text=True, timeout=1
        )
        if res2.returncode == 0:
            val = res2.stdout.strip().strip("'\"").lower()
            if "dark" in val:
                return True
            elif val:
                return False
    except Exception:
        pass
    return None


def _system_is_dark(parent: wx.Window | None = None) -> bool:
    """
    True when the OS or KiCad appearance is dark.

    Checks:
    1. Parent KiCad frame background luminance when embedded.
    2. KiCad user configuration (kicad_common.json: app_theme 2=dark, 1=light).
    3. Linux desktop dark-theme preference (gsettings color-scheme / gtk-theme).
    4. wx.SystemSettings.GetAppearance().IsDark() (supported on Cocoa, MSW, GTK3).
    5. Fallback system window color luminance (wx.SYS_COLOUR_WINDOW).
    """
    # 1. Parent window luminance if embedded in KiCad
    if parent:
        try:
            bg = parent.GetBackgroundColour()
            if bg and bg.IsOk() and bg != wx.NullColour:
                lum = 0.299 * bg.Red() + 0.587 * bg.Green() + 0.114 * bg.Blue()
                return lum < 128
        except Exception:
            pass

    # 2. When running inside KiCad (pcbnew active) or parent present, KiCad config takes precedence
    if has_pcbnew or parent:
        kc_theme = _detect_kicad_theme()
        if kc_theme == "dark":
            return True
        elif kc_theme == "light":
            return False

    # 3. Linux desktop preference (if on Linux and KiCad set to follow system)
    linux_dark = _detect_linux_system_dark()
    if linux_dark is not None:
        return linux_dark

    # 4. Standard cross-platform OS appearance query (works natively on Cocoa, MSW, GTK3)
    try:
        app = wx.SystemSettings.GetAppearance()
        if hasattr(app, "IsDark"):
            return bool(app.IsDark())
    except (NotImplementedError, RuntimeError):
        pass
    except Exception:
        pass

    # 5. Fallback system window color luminance
    try:
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        luminance = 0.299 * bg.Red() + 0.587 * bg.Green() + 0.114 * bg.Blue()
        return luminance < 128
    except (NotImplementedError, RuntimeError):
        pass
    except Exception:
        pass

    # 6. KiCad config fallback for standalone runs if system query gave no answer
    kc_theme = _detect_kicad_theme()
    if kc_theme == "dark":
        return True
    elif kc_theme == "light":
        return False

    return True


def active_palette_mode() -> str:
    """Return the palette currently applied: ``"dark"`` or ``"light"``."""
    return _palette_mode


def refresh_palette(parent: wx.Window | None = None) -> str:
    """
    Point :data:`_COLORS` at the ramp matching the current system appearance.

    Call before building widgets and again on ``wx.EVT_SYS_COLOUR_CHANGED``.
    Returns the mode applied. Safe to call before a ``wx.App`` exists -- it
    falls back to dark rather than raising.
    """
    global _palette_mode
    _palette_mode = "dark" if _system_is_dark(parent) else "light"
    _COLORS.update(_DARK_PALETTE if _palette_mode == "dark" else _LIGHT_PALETTE)
    _MSG_ICON_COLORS.update(_MSG_ICON_COLORS_BY_MODE[_palette_mode])
    return _palette_mode

_BUTTON_RADIUS = 6


def _first_line(text: str, limit: int = 90) -> str:
    """First line of a message, trimmed -- the dialog reports one line."""
    line = (text or "").strip().splitlines()
    first = line[0] if line else ""
    return first if len(first) <= limit else first[: limit - 1] + "\u2026"


def _pointer_is_inside(window: wx.Window) -> bool:
    """
    True when the mouse pointer is over ``window`` right now.

    Shared by the custom-painted controls because hover has to be answered from
    the pointer's real position, not from the last enter/leave event: while a
    control holds the mouse capture the platform stops delivering
    EVT_LEAVE_WINDOW, so the event-driven flag goes stale.
    """
    try:
        return window.ClientRect.Contains(window.ScreenToClient(wx.GetMousePosition()))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cross-Platform UI Hardware / Platform Abstraction Layer (UI HAL)
# ---------------------------------------------------------------------------
# Windows (wxMSW / Direct2D / GDI), Linux (wxGTK3 / Cairo), and macOS (wxMac / Cocoa)
# have distinct rendering pipelines, clipping rules, background erasing, and font DPI
# metrics. To ensure identical pixel-perfect aesthetics across all three operating systems
# without platform-specific forks, all custom widgets delegate low-level rendering,
# color resolution, font measurement, and geometry clipping to these shared HAL functions.

def _hal_resolve_bg(window: wx.Window, fallback: wx.Colour | None = None) -> wx.Colour:
    """
    Safely resolve a window's parent background color across MSW, GTK, and Cocoa.

    On Linux (wxGTK), un-realized parent windows or scrolled viewports can return
    wx.NullColour or uninitialized system background brushes. This climbs the window
    hierarchy until an initialized, valid background color is encountered, falling
    back to the current palette surface/app background.
    """
    parent = window.GetParent() if window else None
    while parent:
        try:
            col = parent.GetBackgroundColour()
            if col and col.IsOk() and col != wx.NullColour:
                return col
        except Exception:
            pass
        parent = parent.GetParent()
    return fallback or _COLORS.get("surface") or _COLORS["app_bg"]


def _hal_measure_text(window: wx.Window | None, text: str, font: wx.Font | None = None) -> tuple[int, int]:
    """
    Measure text dimensions safely without allocating an un-realized wx.ClientDC.

    On Linux (wxGTK3), allocating wx.ClientDC inside widget __init__ before the native
    X11/Wayland GdkWindow is realized produces GTK critical assertions and inaccurate
    extents. Using window.GetTextExtent() queries Pango/GDI/CoreText metrics directly.
    Safely falls back to wx.ScreenDC() if the window is unmapped, unrealized, or returns zero font extents.
    """
    if not text:
        return 0, 0
    try:
        if window:
            if font and font.IsOk():
                w, h = window.GetTextExtent(text, font=font)
            else:
                w, h = window.GetTextExtent(text)
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    try:
        sdc = wx.ScreenDC()
        if font and font.IsOk():
            sdc.SetFont(font)
        elif window:
            f = window.GetFont()
            if f and f.IsOk():
                sdc.SetFont(f)
        w, h = sdc.GetTextExtent(text)
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return len(text) * 7, 14


def _hal_init_paint_dc(window: wx.Window) -> tuple[wx.DC, int, int, wx.Colour]:
    """
    Initialize a double-buffered paint DC, drawable client bounds, and background color.

    On Windows (wxMSW), wx.AutoBufferedPaintDC provides double buffering and eliminates
    flicker. On Linux (wxGTK) and macOS (wxMac), double-buffering is native and
    AutoBufferedPaintDC acts as a standard PaintDC. Clears the background with the
    resolved parent color so transparent corners and anti-aliased edges blend seamlessly.
    """
    dc = wx.AutoBufferedPaintDC(window)
    try:
        width, height = window.GetClientSize()
    except Exception:
        width, height = window.GetSize()
    if width <= 0 or height <= 0:
        width, height = window.GetSize()

    parent_bg = _hal_resolve_bg(window)
    dc.SetBackground(wx.Brush(parent_bg))
    dc.Clear()
    return dc, width, height, parent_bg


def _hal_draw_rounded_rect(
    dc: wx.DC,
    x: int | float,
    y: int | float,
    w: int | float,
    h: int | float,
    radius: int | float,
    fill: wx.Colour,
    border: wx.Colour | None = None,
    border_width: int = 1,
) -> None:
    """
    Draw a filled rounded rectangle with an optional inset border.

    Uses concentric solid fills rather than stroked outlines: Cairo (Linux), Direct2D (Windows),
    and CoreGraphics (macOS) calculate 1px boundary strokes with differing half-pixel offsets.
    Drawing the outer border as a solid filled rounded rectangle with the inner content inset
    on top guarantees pixel-identical geometry across all backends.
    """
    if w <= 0 or h <= 0:
        return
    b_col = border if border is not None else fill
    gc = wx.GraphicsContext.Create(dc)
    if gc:
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(b_col))
        outer = gc.CreatePath()
        outer.AddRoundedRectangle(x, y, w, h, radius)
        gc.DrawPath(outer)

        if fill != b_col and border_width > 0:
            inner_x = x + border_width
            inner_y = y + border_width
            inner_w = max(0, w - border_width * 2)
            inner_h = max(0, h - border_width * 2)
            inner_r = max(0, radius - border_width)
            if inner_w > 0 and inner_h > 0:
                gc.SetBrush(wx.Brush(fill))
                inner = gc.CreatePath()
                inner.AddRoundedRectangle(inner_x, inner_y, inner_w, inner_h, inner_r)
                gc.DrawPath(inner)
    else:
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(b_col))
        dc.DrawRoundedRectangle(int(x), int(y), int(w), int(h), int(radius))
        if fill != b_col and border_width > 0:
            inner_x = int(x + border_width)
            inner_y = int(y + border_width)
            inner_w = max(0, int(w - border_width * 2))
            inner_h = max(0, int(h - border_width * 2))
            inner_r = max(0, int(radius - border_width))
            if inner_w > 0 and inner_h > 0:
                dc.SetBrush(wx.Brush(fill))
                dc.DrawRoundedRectangle(inner_x, inner_y, inner_w, inner_h, inner_r)


def _hal_draw_circle(
    dc: wx.DC,
    cx: float,
    cy: float,
    radius: float,
    fill: wx.Colour,
    border: wx.Colour | None = None,
    border_width: int = 1,
) -> None:
    """Draw a filled circle with an optional inset border for radio buttons and status discs."""
    if radius <= 0:
        return
    b_col = border if border is not None else fill
    gc = wx.GraphicsContext.Create(dc)
    if gc:
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(b_col))
        gc.DrawEllipse(cx - radius, cy - radius, radius * 2, radius * 2)
        if fill != b_col and border_width > 0:
            inner_r = max(0, radius - border_width)
            if inner_r > 0:
                gc.SetBrush(wx.Brush(fill))
                gc.DrawEllipse(cx - inner_r, cy - inner_r, inner_r * 2, inner_r * 2)
    else:
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(b_col))
        dc.DrawEllipse(int(cx - radius), int(cy - radius), int(radius * 2), int(radius * 2))
        if fill != b_col and border_width > 0:
            inner_r = max(0, int(radius - border_width))
            if inner_r > 0:
                dc.SetBrush(wx.Brush(fill))
                dc.DrawEllipse(int(cx - inner_r), int(cy - inner_r), int(inner_r * 2), int(inner_r * 2))


def _hal_control_border(selected: bool, hover: bool) -> tuple[wx.Colour, int]:
    """
    Resolve cross-platform border colour and stroke width for checkboxes and radio glyphs.

    Ensures identical aesthetics across macOS, Windows, and Linux:
    - Selected / checked controls use the brand accent color.
    - Unselected / unchecked controls use neutral border (or muted on hover).
    - Eliminates aberrant selection outlines on unselected controls.
    """
    if selected:
        return _COLORS["accent"], 1
    if hover:
        return _COLORS["muted"], 1
    return _COLORS["border"], 1


_flat_button_icon_cache: dict[tuple[str, int, str], wx.Bitmap] = {}


def _get_button_icon_bitmap(icon_kind: str, size: int, colour: wx.Colour) -> wx.Bitmap | None:
    """Load and rasterize a Material Symbol icon tinted with colour for flat buttons."""
    name = "lock_open" if icon_kind in ("unlock", "lock_open") else icon_kind
    hex_col = f"#{colour.Red():02x}{colour.Green():02x}{colour.Blue():02x}"
    key = (name, size, hex_col)
    if key in _flat_button_icon_cache:
        cached = _flat_button_icon_cache[key]
        return cached if cached.IsOk() else None

    svg_data = kiforge.fetch_tab_icon_svg(name)
    if not svg_data:
        return None
    try:
        tinted = kiforge.prepare_tab_icon_svg(svg_data, hex_col)
        bundle = wx.BitmapBundle.FromSVG(tinted, (size * 2, size * 2))
        bmp = bundle.GetBitmap(wx.Size(size, size))
        _flat_button_icon_cache[key] = bmp
        return bmp if bmp.IsOk() else None
    except Exception as exc:
        logger.warning("Failed to rasterize button icon %s: %s", name, exc)
        return None


class _FlatButton(wx.Panel):
    """Flat filled button with rounded corners; paints consistently on Windows, Linux, and macOS."""

    def __init__(self, parent, label: str = "", *, primary: bool = False, min_width: int = 0, icon_kind: str | None = None):
        """Initialise flat button with text label, appearance style, and minimum width."""
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._primary = primary
        self._icon_kind = icon_kind
        self._tone = None
        self._hover = False
        self._pressed = False
        self._enabled = True
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_hal_resolve_bg(parent))
        font = self.GetFont()
        if primary:
            font = wx.Font(font)
            font.SetWeight(wx.FONTWEIGHT_BOLD)
        _, th = _hal_measure_text(self, label, font)
        btn_h = max(_CTRL_H, th + _SP_SM)
        self.SetMinSize((min_width, btn_h))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

    def SetTone(self, colour) -> None:
        """Override the primary fill -- used to confirm a successful export in green."""
        self._tone = colour
        self._primary = colour is not None
        self.Refresh()

    def SetLabel(self, label: str) -> None:
        """Change the painted caption -- wx.Panel's own label is not drawn here."""
        self._label = label
        self.Refresh()

    def GetLabel(self) -> str:
        """Return the painted label string."""
        return self._label

    def SetIcon(self, icon_kind: str | None) -> None:
        """Set or clear icon (e.g. 'lock', 'unlock') to render on the button."""
        self._icon_kind = icon_kind
        self.Refresh()

    def _on_enter(self, event):
        """Handle mouse enter event to update hover appearance."""
        if self._enabled:
            self._hover = True
            self.Refresh()
        event.Skip()

    def _on_leave(self, event):
        """Handle mouse leave event to clear hover state."""
        self._hover = False
        self._pressed = False
        self.Refresh()
        event.Skip()

    def _on_left_down(self, event):
        """Handle left mouse button press and capture mouse input."""
        if not self._enabled:
            return
        self._pressed = True
        self.CaptureMouse()
        self.Refresh()

    def _on_left_up(self, event):
        """Handle left mouse button release and fire button event if released inside."""
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        # See _FlatRadioButton._on_left_up: a captured mouse suppresses
        # EVT_LEAVE_WINDOW, so hover has to be recomputed from the pointer.
        inside = self.ClientRect.Contains(event.GetPosition())
        self._hover = inside
        self.Refresh()
        if was_pressed and inside:
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, self.GetId())
            event.SetEventObject(self)
            wx.PostEvent(self, event)

    def _on_capture_lost(self, event):
        """Handle lost mouse capture."""
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _on_paint(self, event):
        """Paint flat rounded button with border, fill tone, and centered label via UI HAL."""
        dc, width, height, parent_bg = _hal_init_paint_dc(self)

        if self._primary:
            accent = self._tone or _COLORS["accent"]
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

        _hal_draw_rounded_rect(dc, 0, 0, width, height, _BUTTON_RADIUS, fill=fill, border=border, border_width=1)

        font = self.GetFont()
        if self._primary and self._enabled:
            font = wx.Font(font)
            font.SetWeight(wx.FONTWEIGHT_BOLD)

        gc = wx.GraphicsContext.Create(dc)
        if self._icon_kind:
            bmp = _get_button_icon_bitmap(self._icon_kind, 18, text)
            if bmp and bmp.IsOk():
                bx = (width - bmp.GetWidth()) / 2.0
                by = (height - bmp.GetHeight()) / 2.0
                if gc:
                    gc.DrawBitmap(bmp, bx, by, bmp.GetWidth(), bmp.GetHeight())
                else:
                    dc.DrawBitmap(bmp, int(bx), int(by), True)
        elif self._label:
            if gc:
                gc.SetFont(font, text)
                gw, gh = gc.GetTextExtent(self._label)
                gx = (width - gw) / 2
                gy = (height - gh) / 2 - 1
                gc.DrawText(self._label, gx, gy)
            else:
                dc.SetFont(font)
                dc.SetTextForeground(text)
                tw, th = _hal_measure_text(self, self._label, font)
                try:
                    m = dc.GetFontMetrics()
                    ty = max(0, (height - m.ascent - m.internalLeading) // 2)
                except Exception:
                    ty = max(0, (height - th) // 2 - 1)
                dc.DrawText(self._label, max(0, (width - tw) // 2), ty)

    def DoGetBestSize(self) -> wx.Size:
        """Calculate button best size based on font extents and padding."""
        font = self.GetFont()
        if self._primary:
            font = wx.Font(font)
            font.SetWeight(wx.FONTWEIGHT_BOLD)
        tw, th = _hal_measure_text(self, self._label, font)
        w = max(self.GetMinSize().width, tw + _SP_MD * 2)
        h = max(self.GetMinSize().height, th + _SP_SM)
        return wx.Size(w, h)

    def Enable(self, enable=True):
        """Enable or disable button interaction and refresh visual style."""
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        """Disable button interaction."""
        return self.Enable(False)


_CHECKBOX_GLYPH_SIZE = 16
_CHECKBOX_GLYPH_RADIUS = 4


def _checkmark_pen() -> "wx.Pen":
    """White stroke for the checkbox tick, with rounded ends."""
    pen = wx.Pen(wx.Colour(255, 255, 255), 2)
    pen.SetCap(wx.CAP_ROUND)
    pen.SetJoin(wx.JOIN_ROUND)
    return pen


class _FlatCheckBox(wx.Panel):
    """
    Fully custom-painted checkbox: consistent pixel-exact rendering on Windows, Linux, and macOS.
    Eliminates native OS focus-rectangle bugs and off-color punchouts.
    """

    def __init__(self, parent, label: str = ""):
        """Initialise custom-painted checkbox with text label and default unchecked state."""
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._checked = False
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._has_focus = False
        self._focus_from_pointer = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_hal_resolve_bg(parent))

        text_w, text_h = _hal_measure_text(self, label, self.GetFont())
        gap = _SP_SM if label else 0
        extra_pad = _SP_SM if label else 0
        self.SetMinSize((_CHECKBOX_GLYPH_SIZE + gap + text_w + extra_pad, max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SET_FOCUS, self._on_set_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def DoGetBestSize(self) -> wx.Size:
        """Calculate best size including glyph, gap, text width, and right padding for ClearType/font overhang."""
        text_w, text_h = _hal_measure_text(self, self._label, self.GetFont())
        gap = _SP_SM if self._label else 0
        extra_pad = _SP_SM if self._label else 0
        return wx.Size(
            _CHECKBOX_GLYPH_SIZE + gap + text_w + extra_pad,
            max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS,
        )

    def AcceptsFocus(self):
        """Return True if enabled to allow keyboard focus navigation."""
        return self._enabled

    def _on_set_focus(self, event):
        """Draw a focus ring for keyboard focus only."""
        self._has_focus = not self._focus_from_pointer
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_kill_focus(self, event):
        """Clear focus state from event."""
        self._has_focus = False
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_key_down(self, event):
        """Toggle checkbox state when Space key is pressed."""
        if self._enabled and event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._toggle()
        else:
            event.Skip()

    def _on_enter(self, event):
        """Update hover highlight state on mouse enter."""
        if self._enabled:
            self._hover = True
            self.Refresh()
        event.Skip()

    def _on_leave(self, event):
        """Clear hover and pressed states on mouse leave."""
        self._hover = False
        self._pressed = False
        self.Refresh()
        event.Skip()

    def _on_left_down(self, event):
        """Handle left mouse click."""
        if not self._enabled:
            return
        self._focus_from_pointer = True
        self.SetFocus()
        self._pressed = True
        self.CaptureMouse()
        self.Refresh()

    def _on_left_up(self, event):
        """Handle left mouse release and toggle if inside."""
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        inside = self.ClientRect.Contains(event.GetPosition())
        self._hover = inside
        self.Refresh()
        if was_pressed and inside:
            self._toggle()

    def _on_capture_lost(self, event):
        """Handle lost mouse capture."""
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _toggle(self):
        """Toggle checked state and fire wx.EVT_CHECKBOX."""
        self._checked = not self._checked
        self.Refresh()
        event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, self.GetId())
        event.SetEventObject(self)
        event.SetInt(1 if self._checked else 0)
        wx.PostEvent(self, event)

    def _on_paint(self, event):
        """Paint custom square checkbox glyph, focus ring, checkmark, and text label via UI HAL."""
        dc, width, height, parent_bg = _hal_init_paint_dc(self)

        box_y = (height - _CHECKBOX_GLYPH_SIZE) // 2
        accent = _COLORS["accent"]

        if not self._enabled:
            if self._checked:
                fill = wx.Colour(accent.Red() // 2, accent.Green() // 2, accent.Blue() // 2)
                border = fill
            else:
                fill = _COLORS["input_bg"]
                border = _COLORS["border"]
            text_colour = _COLORS["muted"]
            border_width = 1
        elif self._checked:
            fill = accent
            border, border_width = _hal_control_border(selected=True, hover=self._hover)
            text_colour = _COLORS["text"]
        else:
            fill = _COLORS["input_bg"]
            border, border_width = _hal_control_border(selected=False, hover=self._hover)
            text_colour = _COLORS["text"]

        _hal_draw_rounded_rect(
            dc,
            0,
            box_y,
            _CHECKBOX_GLYPH_SIZE,
            _CHECKBOX_GLYPH_SIZE,
            _CHECKBOX_GLYPH_RADIUS,
            fill=fill,
            border=border,
            border_width=border_width,
        )

        if self._checked:
            g = _CHECKBOX_GLYPH_SIZE
            gc = wx.GraphicsContext.Create(dc)
            if gc:
                gc.SetPen(_checkmark_pen())
                check = gc.CreatePath()
                check.MoveToPoint(0.22 * g, box_y + 0.5 * g)
                check.AddLineToPoint(0.42 * g, box_y + 0.72 * g)
                check.AddLineToPoint(0.78 * g, box_y + 0.28 * g)
                gc.StrokePath(check)
            else:
                dc.SetPen(_checkmark_pen())
                dc.DrawLine(int(0.22 * g), int(box_y + 0.5 * g), int(0.42 * g), int(box_y + 0.72 * g))
                dc.DrawLine(int(0.42 * g), int(box_y + 0.72 * g), int(0.78 * g), int(box_y + 0.28 * g))

        if self._label:
            dc.SetTextForeground(text_colour)
            dc.SetFont(self.GetFont())
            _tw, text_h = _hal_measure_text(self, self._label, self.GetFont())
            dc.DrawText(self._label, _CHECKBOX_GLYPH_SIZE + _SP_SM, max(0, (height - text_h) // 2))

    # --- wx.CheckBox-compatible API ---
    def IsChecked(self) -> bool:
        """Return True if checkbox is checked."""
        return self._checked

    def GetValue(self) -> bool:
        """Return True if checkbox is checked."""
        return self._checked

    def SetValue(self, value: bool) -> None:
        """Programmatic state update -- matches wx.CheckBox.SetValue(): no event fired."""
        if self._checked != bool(value):
            self._checked = bool(value)
            self.Refresh()

    def Enable(self, enable=True):
        """Enable or disable interaction and repaint control."""
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        """Disable interaction."""
        return self.Enable(False)


class _FlatRadioButton(wx.Panel):
    """
    Fully custom-painted radio button: consistent circular glyph across Windows, Linux, and macOS.
    Eliminates OS punchout halo artifacts and unselected disabled dots.
    """

    def __init__(self, parent, label: str = "", *, group: list | None = None):
        """Initialise custom-painted radio button with text label and mutual exclusion group."""
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._selected = False
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._has_focus = False
        self._focus_from_pointer = False
        self._group = group if group is not None else [self]
        if group is not None:
            group.append(self)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_hal_resolve_bg(parent))

        text_w, text_h = _hal_measure_text(self, label, self.GetFont())
        gap = _SP_SM if label else 0
        extra_pad = _SP_SM if label else 0
        self.SetMinSize((_CHECKBOX_GLYPH_SIZE + gap + text_w + extra_pad, max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SET_FOCUS, self._on_set_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def DoGetBestSize(self) -> wx.Size:
        """Calculate best size including glyph, gap, text width, and right padding for ClearType/font overhang."""
        text_w, text_h = _hal_measure_text(self, self._label, self.GetFont())
        gap = _SP_SM if self._label else 0
        extra_pad = _SP_SM if self._label else 0
        return wx.Size(
            _CHECKBOX_GLYPH_SIZE + gap + text_w + extra_pad,
            max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS,
        )

    def AcceptsFocus(self):
        """Return True if enabled to allow keyboard focus navigation."""
        return self._enabled

    def AcceptsFocusFromKeyboard(self):
        """Only the selected radio button in a group acts as a Tab stop."""
        if not self._enabled:
            return False
        selected_in_group = [b for b in self._group if b._selected and b._enabled]
        if selected_in_group:
            return self is selected_in_group[0]
        enabled_in_group = [b for b in self._group if b._enabled]
        return bool(enabled_in_group and self is enabled_in_group[0])

    def _on_set_focus(self, event):
        """Draw a focus ring for keyboard focus only."""
        self._has_focus = not self._focus_from_pointer
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_kill_focus(self, event):
        """Clear focus state from event."""
        self._has_focus = False
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_key_down(self, event):
        """Select radio button on Space/Enter, or navigate group with Arrow keys."""
        if not self._enabled:
            event.Skip()
            return
        key = event.GetKeyCode()
        if key in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._select()
        elif key in (wx.WXK_UP, wx.WXK_LEFT):
            self._navigate_group(-1)
        elif key in (wx.WXK_DOWN, wx.WXK_RIGHT):
            self._navigate_group(1)
        else:
            event.Skip()

    def _navigate_group(self, direction: int):
        """Navigate and select adjacent radio button in the group."""
        if not self._group or len(self._group) <= 1:
            return
        try:
            curr_idx = self._group.index(self)
        except ValueError:
            return
        next_idx = (curr_idx + direction) % len(self._group)
        target = self._group[next_idx]
        if target.IsEnabled():
            target.SetFocus()
            target._select()

    def _on_enter(self, event):
        """Update hover highlight state on mouse enter."""
        if self._enabled:
            self._hover = True
            self.Refresh()
        event.Skip()

    def _on_leave(self, event):
        """Clear hover and pressed states on mouse leave."""
        self._hover = False
        self._pressed = False
        self.Refresh()
        event.Skip()

    def _on_left_down(self, event):
        """Handle left mouse click."""
        if not self._enabled:
            return
        self._focus_from_pointer = True
        self.SetFocus()
        self._pressed = True
        self.CaptureMouse()
        self.Refresh()

    def _on_left_up(self, event):
        """Handle left mouse release and select if inside."""
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        inside = self.ClientRect.Contains(event.GetPosition())
        self._hover = inside
        self.Refresh()
        if was_pressed and inside:
            self._select()

    def _on_capture_lost(self, event):
        """Handle lost mouse capture."""
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _apply_selection(self):
        """Select this radio and deselect other siblings in the group."""
        for other in self._group:
            if other is not self and other._selected:
                other._selected = False
                other.Refresh()
        self._selected = True
        self.Refresh()

    def _select(self):
        """User-driven selection (click/Space/Enter): update state and notify."""
        if self._selected:
            return
        self._apply_selection()
        event = wx.CommandEvent(wx.EVT_RADIOBUTTON.typeId, self.GetId())
        event.SetEventObject(self)
        wx.PostEvent(self, event)

    def _on_paint(self, event):
        """Paint custom circular radio glyph, focus ring, and selected dot via UI HAL."""
        dc, width, height, parent_bg = _hal_init_paint_dc(self)

        box_y = (height - _CHECKBOX_GLYPH_SIZE) // 2
        accent = _COLORS["accent"]
        cx = _CHECKBOX_GLYPH_SIZE / 2
        cy = box_y + _CHECKBOX_GLYPH_SIZE / 2
        r = _CHECKBOX_GLYPH_SIZE / 2 - 1

        if not self._enabled:
            border = _COLORS["border"]
            dot = _COLORS["muted"] if self._selected else None
            text_colour = _COLORS["muted"]
            border_width = 1
        elif self._selected:
            border, border_width = _hal_control_border(selected=True, hover=self._hover)
            dot = accent
            text_colour = _COLORS["text"]
        else:
            border, border_width = _hal_control_border(selected=False, hover=self._hover)
            dot = None
            text_colour = _COLORS["text"]

        _hal_draw_circle(dc, cx, cy, r, fill=parent_bg, border=border, border_width=border_width)

        if dot is not None:
            dr = round(r * 0.5)
            _hal_draw_circle(dc, cx, cy, dr, fill=dot, border=dot, border_width=0)

        if self._label:
            dc.SetTextForeground(text_colour)
            dc.SetFont(self.GetFont())
            _tw, text_h = _hal_measure_text(self, self._label, self.GetFont())
            dc.DrawText(self._label, _CHECKBOX_GLYPH_SIZE + _SP_SM, max(0, (height - text_h) // 2))

    # --- wx.RadioButton-compatible API ---
    def GetValue(self) -> bool:
        """Return True if radio button is selected."""
        return self._selected

    def SetValue(self, value: bool) -> None:
        """Programmatic selection -- matches wx.RadioButton.SetValue(): no event fired."""
        if value:
            self._apply_selection()
        elif self._selected:
            self._selected = False
            self.Refresh()

    def Enable(self, enable=True):
        """Enable or disable interaction and repaint control."""
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        """Disable interaction."""
        return self.Enable(False)


class _FlatChoicePopup(wx.PopupTransientWindow):
    """
    Custom popup transient window for _FlatChoice matching the application design system.
    Eliminates native Win32 context menus, bullet dots, and misaligned text.
    """

    def __init__(self, parent_choice: "_FlatChoice", choices: list[str], selected_index: int):
        super().__init__(parent_choice.GetTopLevelParent(), flags=wx.BORDER_NONE)
        self._choice_ctrl = parent_choice
        self._choices = choices
        self._selected = selected_index
        self._hover_idx = selected_index

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

        font = parent_choice.GetFont()
        max_h = 0
        for ch in choices:
            _, th = _hal_measure_text(parent_choice, ch, font)
            max_h = max(max_h, th)
        self._row_h = max(_CTRL_H, max_h + _SP_SM)
        total_h = self._row_h * len(choices) + 6
        choice_w = parent_choice.GetSize().width
        self.SetSize((choice_w, total_h))

    def OnDismiss(self):
        if hasattr(self, "_choice_ctrl") and self._choice_ctrl:
            self._choice_ctrl._dismiss_time = time.time()
            self._choice_ctrl._popup = None
            self._choice_ctrl._popup_open = False
            self._choice_ctrl.Refresh()
        super().OnDismiss()

    def Dismiss(self):
        if hasattr(self, "_choice_ctrl") and self._choice_ctrl:
            self._choice_ctrl._dismiss_time = time.time()
            self._choice_ctrl._popup = None
            self._choice_ctrl._popup_open = False
            self._choice_ctrl.Refresh()
        super().Dismiss()

    def _item_index_at_y(self, y: int) -> int:
        idx = (y - 3) // self._row_h
        if 0 <= idx < len(self._choices):
            return idx
        return -1

    def _on_motion(self, event):
        idx = self._item_index_at_y(event.GetPosition().y)
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.Refresh()

    def _on_leave(self, event):
        self._hover_idx = -1
        self.Refresh()

    def _on_left_down(self, event):
        idx = self._item_index_at_y(event.GetPosition().y)
        if 0 <= idx < len(self._choices):
            self._hover_idx = idx
            self.Refresh()
        event.Skip()

    def _on_left_up(self, event):
        idx = self._item_index_at_y(event.GetPosition().y)
        if 0 <= idx < len(self._choices):
            self._choice_ctrl._on_popup_item_chosen(idx)
        self.Dismiss()

    def _on_key_down(self, event):
        kc = event.GetKeyCode()
        if kc == wx.WXK_ESCAPE:
            self.Dismiss()
        elif kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            if 0 <= self._hover_idx < len(self._choices):
                self._choice_ctrl._on_popup_item_chosen(self._hover_idx)
            self.Dismiss()
        elif kc == wx.WXK_UP:
            self._hover_idx = max(0, (self._hover_idx if self._hover_idx >= 0 else self._selected) - 1)
            self.Refresh()
        elif kc == wx.WXK_DOWN:
            self._hover_idx = min(len(self._choices) - 1, (self._hover_idx if self._hover_idx >= 0 else self._selected) + 1)
            self.Refresh()
        else:
            event.Skip()

    def _on_paint(self, event):
        dc, width, height, _ = _hal_init_paint_dc(self)
        bg = _COLORS.get("surface") or _COLORS["app_bg"]
        border = _COLORS["border"]

        # Outer rounded box
        _hal_draw_rounded_rect(dc, 0, 0, width, height, _BUTTON_RADIUS, fill=bg, border=border, border_width=1)

        base_font = self.GetFont()
        y = 3
        item_padding_x = 5
        for idx, text in enumerate(self._choices):
            is_selected = (idx == self._selected)
            is_hover = (idx == self._hover_idx)

            row_rect_w = width - 6
            row_x = 3
            row_y = y

            # Hover / Selected highlight pill
            if is_hover:
                fill_col = _COLORS["border"]
                _hal_draw_rounded_rect(dc, row_x, row_y, row_rect_w, self._row_h, _CHECKBOX_GLYPH_RADIUS, fill=fill_col, border=None)
            elif is_selected:
                fill_col = _COLORS.get("input_bg", bg)
                _hal_draw_rounded_rect(dc, row_x, row_y, row_rect_w, self._row_h, _CHECKBOX_GLYPH_RADIUS, fill=fill_col, border=None)

            # High-contrast text: crisp readable white across dark backgrounds, bold when selected
            text_col = _COLORS["text"]
            if is_selected:
                draw_font = wx.Font(base_font)
                draw_font.SetWeight(wx.FONTWEIGHT_BOLD)
            else:
                draw_font = base_font

            gc = wx.GraphicsContext.Create(dc)
            if gc:
                gc.SetFont(draw_font, text_col)
                _gw, gh = gc.GetTextExtent(text)
                text_y = row_y + (self._row_h - gh) / 2 - 1
                gc.DrawText(text, row_x + item_padding_x, text_y)

                if is_selected:
                    chk_x = width - _SP_MD - 8
                    chk_y = row_y + self._row_h // 2
                    pen = wx.Pen(_COLORS["accent"], 2)
                    pen.SetCap(wx.CAP_ROUND)
                    pen.SetJoin(wx.JOIN_ROUND)
                    gc.SetPen(pen)
                    path = gc.CreatePath()
                    path.MoveToPoint(chk_x - 3, chk_y)
                    path.AddLineToPoint(chk_x, chk_y + 3)
                    path.AddLineToPoint(chk_x + 5, chk_y - 3)
                    gc.StrokePath(path)
            else:
                dc.SetFont(draw_font)
                dc.SetTextForeground(text_col)
                _tw, th = _hal_measure_text(self, text, draw_font)
                text_y = row_y + max(0, (self._row_h - th) // 2)
                dc.DrawText(text, row_x + item_padding_x, text_y)

            y += self._row_h


class _FlatChoice(wx.Panel):
    """
    Flat custom dropdown choice control with rounded corners and dark/light theming.
    Replaces native Win32 wx.Choice which cannot be styled on Windows, ensuring
    identical modern aesthetics across Windows, macOS, and Linux.
    """

    def __init__(
        self,
        parent,
        id: int = wx.ID_ANY,
        pos=wx.DefaultPosition,
        size=wx.DefaultSize,
        choices: list[str] | None = None,
        style: int = 0,
        name: str = "flatChoice",
    ):
        super().__init__(parent, id, pos=pos, size=size, style=wx.TAB_TRAVERSAL | wx.BORDER_NONE, name=name)
        self._choices = list(choices) if choices else []
        self._selection = 0 if self._choices else wx.NOT_FOUND
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._has_focus = False
        self._focus_from_pointer = False
        self._popup_open = False
        self._dismiss_time = 0.0
        self._popup = None

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(_hal_resolve_bg(parent))

        # Determine min size based on choices
        if isinstance(size, (tuple, list)):
            size = wx.Size(*size)
        font = self.GetFont()
        max_w = 0
        max_h = 0
        for ch in self._choices:
            tw, th = _hal_measure_text(self, ch, font)
            max_w = max(max_w, tw)
            max_h = max(max_h, th)
        min_w = max(90, max_w + 36)
        ctrl_h = max(_CTRL_H, max_h + _SP_SM)
        req_w = size.width if (hasattr(size, "width") and size.width > 0) else min_w
        req_h = size.height if (hasattr(size, "height") and size.height > 0) else ctrl_h
        self.SetMinSize((req_w, req_h))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_SET_FOCUS, self._on_set_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def DoGetBestSize(self) -> wx.Size:
        font = self.GetFont()
        max_w = 0
        max_h = 0
        for ch in self._choices:
            tw, th = _hal_measure_text(self, ch, font)
            max_w = max(max_w, tw)
            max_h = max(max_h, th)
        return wx.Size(max(90, max_w + 36), max(_CTRL_H, max_h + _SP_SM))

    def AcceptsFocus(self) -> bool:
        return self._enabled

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

    def _on_set_focus(self, event):
        self._has_focus = not self._focus_from_pointer
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_kill_focus(self, event):
        self._has_focus = False
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_left_down(self, event):
        if not self._enabled:
            return
        self._focus_from_pointer = True
        self.SetFocus()
        self._pressed = True
        self.Refresh()
        self._show_popup()

    def _on_left_up(self, event):
        if not self._enabled:
            return
        self._pressed = False
        self.Refresh()

    def _on_capture_lost(self, event):
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _on_key_down(self, event):
        if not self._enabled:
            event.Skip()
            return
        kc = event.GetKeyCode()
        if kc in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_DOWN) and (kc != wx.WXK_DOWN or event.AltDown()):
            self._show_popup()
        elif kc == wx.WXK_UP:
            if self._selection > 0:
                self.SetSelection(self._selection - 1)
                self._notify_choice()
        elif kc == wx.WXK_DOWN:
            if self._selection < len(self._choices) - 1:
                self.SetSelection(self._selection + 1)
                self._notify_choice()
        else:
            event.Skip()

    def _show_popup(self):
        """Show custom styled popup window matching app theme."""
        if not self._choices:
            return
        if time.time() - getattr(self, "_dismiss_time", 0.0) < 0.25:
            return

        try:
            popup = _FlatChoicePopup(self, self._choices, self._selection)
            self._popup = popup
            self._popup_open = True
            self.Refresh()

            w, h = self.GetSize()
            total_h = popup.GetSize().height
            screen_pt = self.ClientToScreen(wx.Point(0, h))
            popup.SetSize((w, total_h))

            disp_idx = wx.Display.GetFromPoint(screen_pt)
            if disp_idx != wx.NOT_FOUND:
                client_rect = wx.Display(disp_idx).GetClientArea()
                if screen_pt.y + total_h > client_rect.GetBottom():
                    screen_pt = self.ClientToScreen(wx.Point(0, -total_h))

            popup.Move(screen_pt)
            popup.Popup()
        except Exception as exc:
            self._popup_open = False
            self.Refresh()
            logger.exception("Failed to open _FlatChoice custom popup: %s", exc)
            self._show_fallback_menu()

    def _show_fallback_menu(self):
        """Fallback popup menu if transient window encounters window manager restrictions."""
        menu = wx.Menu()
        for idx, text in enumerate(self._choices):
            item = menu.Append(wx.ID_ANY, text)
            menu.Bind(wx.EVT_MENU, lambda evt, i=idx: self._on_popup_item_chosen(i), item)
        self.PopupMenu(menu, (0, self.GetSize().height))
        menu.Destroy()

    def _on_popup_item_chosen(self, index: int):
        if 0 <= index < len(self._choices) and index != self._selection:
            self._selection = index
            self.Refresh()
            self._notify_choice()

    def _notify_choice(self):
        """Post a wx.EVT_CHOICE notification."""
        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, self.GetId())
        event.SetEventObject(self)
        event.SetInt(self._selection)
        if 0 <= self._selection < len(self._choices):
            event.SetString(self._choices[self._selection])
        self.GetEventHandler().ProcessEvent(event)

    def _on_paint(self, event):
        """Paint flat choice container, current selection text, and dropdown chevron."""
        dc, width, height, parent_bg = _hal_init_paint_dc(self)

        focused = self._has_focus and self._enabled
        is_open = getattr(self, "_popup_open", False)

        if not self._enabled:
            fill = _COLORS["input_bg"]
            border = _COLORS["border"]
            text_colour = _COLORS["muted"]
            chevron_colour = _COLORS["border"]
        elif is_open or self._pressed:
            fill = _COLORS["input_bg"]
            border = _COLORS["muted"]
            text_colour = _COLORS["input_fg"]
            chevron_colour = _COLORS["text"]
        elif self._hover or focused:
            fill = _COLORS["input_bg"]
            border = _COLORS["muted"]
            text_colour = _COLORS["input_fg"]
            chevron_colour = _COLORS["text"]
        else:
            fill = _COLORS["input_bg"]
            border = _COLORS["border"]
            text_colour = _COLORS["input_fg"]
            chevron_colour = _COLORS["muted"]

        border_w = 1
        _hal_draw_rounded_rect(
            dc, 0, 0, width, height, _BUTTON_RADIUS, fill=fill, border=border, border_width=border_w
        )

        label = self.GetStringSelection()
        if label:
            font = self.GetFont()
            gc = wx.GraphicsContext.Create(dc)
            if gc:
                gc.SetFont(font, text_colour)
                _gw, gh = gc.GetTextExtent(label)
                gy = (height - gh) / 2 - 1
                gc.DrawText(label, _SP_SM, gy)
            else:
                dc.SetFont(font)
                dc.SetTextForeground(text_colour)
                tw, th = _hal_measure_text(self, label, font)
                try:
                    m = dc.GetFontMetrics()
                    ty = max(0, (height - m.ascent - m.internalLeading) // 2)
                except Exception:
                    ty = max(0, (height - th) // 2 - 1)
                dc.DrawText(label, _SP_SM, ty)

        arrow_cx = width - _SP_MD
        arrow_cy = height // 2
        gc = wx.GraphicsContext.Create(dc)
        if gc:
            pen = wx.Pen(chevron_colour, 2)
            pen.SetCap(wx.CAP_ROUND)
            pen.SetJoin(wx.JOIN_ROUND)
            gc.SetPen(pen)
            path = gc.CreatePath()
            if is_open:
                path.MoveToPoint(arrow_cx - 4, arrow_cy + 2)
                path.AddLineToPoint(arrow_cx, arrow_cy - 2)
                path.AddLineToPoint(arrow_cx + 4, arrow_cy + 2)
            else:
                path.MoveToPoint(arrow_cx - 4, arrow_cy - 2)
                path.AddLineToPoint(arrow_cx, arrow_cy + 2)
                path.AddLineToPoint(arrow_cx + 4, arrow_cy - 2)
            gc.StrokePath(path)
        else:
            dc.SetPen(wx.Pen(chevron_colour, 1))
            if is_open:
                dc.DrawLine(int(arrow_cx - 4), int(arrow_cy + 2), int(arrow_cx), int(arrow_cy - 2))
                dc.DrawLine(int(arrow_cx), int(arrow_cy - 2), int(arrow_cx + 4), int(arrow_cy + 2))
            else:
                dc.DrawLine(int(arrow_cx - 4), int(arrow_cy - 2), int(arrow_cx), int(arrow_cy + 2))
                dc.DrawLine(int(arrow_cx), int(arrow_cy + 2), int(arrow_cx + 4), int(arrow_cy - 2))

    # --- wx.Choice-compatible API ---
    def GetSelection(self) -> int:
        return self._selection

    def SetSelection(self, n: int) -> None:
        if 0 <= n < len(self._choices):
            self._selection = n
            self.Refresh()
        elif n == wx.NOT_FOUND:
            self._selection = wx.NOT_FOUND
            self.Refresh()

    def GetStringSelection(self) -> str:
        if 0 <= self._selection < len(self._choices):
            return self._choices[self._selection]
        return ""

    def SetStringSelection(self, string: str) -> bool:
        if string in self._choices:
            self.SetSelection(self._choices.index(string))
            return True
        return False

    def GetString(self, n: int) -> str:
        if 0 <= n < len(self._choices):
            return self._choices[n]
        return ""

    def SetString(self, n: int, string: str) -> None:
        if 0 <= n < len(self._choices):
            self._choices[n] = string
            self.Refresh()

    def GetCount(self) -> int:
        return len(self._choices)

    def FindString(self, string: str, caseSensitive: bool = False) -> int:
        for idx, ch in enumerate(self._choices):
            if (ch == string) if caseSensitive else (ch.lower() == string.lower()):
                return idx
        return wx.NOT_FOUND

    def Append(self, item: str) -> int:
        self._choices.append(item)
        if self._selection == wx.NOT_FOUND:
            self._selection = 0
        self.Refresh()
        return len(self._choices) - 1

    def Clear(self) -> None:
        self._choices.clear()
        self._selection = wx.NOT_FOUND
        self.Refresh()

    def Enable(self, enable: bool = True) -> bool:
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self) -> bool:
        return self.Enable(False)


def _format_dialog_message(msg: str, max_token: int = 36) -> str:
    """Format message text so long paths and unbroken tokens wrap cleanly on path separators."""
    if not msg:
        return ""

    def _split_long_token(match: re.Match) -> str:
        tok = match.group(0)
        if len(tok) <= max_token:
            return tok
        # Split on path separators (\ or /) without losing them
        parts = re.split(r"([\\/])", tok)
        out: list[str] = []
        cur = ""
        for p in parts:
            if len(cur) + len(p) > max_token and cur:
                out.append(cur)
                cur = p
            else:
                cur += p
        if cur:
            out.append(cur)
        return "\n".join(out)

    return re.sub(r"\S+", _split_long_token, msg)


class _ExportProgressDialog(wx.Dialog):
    """Non-modal export progress window following the system appearance."""

    def __init__(self, parent, on_cancel=None):
        """Initialise export progress dialog with gauge, message label, and cancel button."""
        super().__init__(
            parent,
            title="KiForge",
            style=(wx.DEFAULT_DIALOG_STYLE & ~wx.MINIMIZE_BOX & ~wx.MAXIMIZE_BOX),
        )
        self._finished = False
        # Invoked the moment Cancel is pressed, not on the next poll tick. The
        # poll timer only runs when the GUI thread is free, and a long export
        # step can starve it -- which is exactly when a user reaches for
        # Cancel and finds it does nothing.
        self._on_cancel_requested = on_cancel
        refresh_palette()
        self._cancelled = False
        self._value = -1
        self._message = ""
        self.SetBackgroundColour(_COLORS["app_bg"])
        sizer = wx.BoxSizer(wx.VERTICAL)
        pad_h = _SP_LG
        self.lbl_message = wx.StaticText(self, label="Initializing exporter…")
        self.lbl_message.SetForegroundColour(_COLORS["text"])
        sizer.Add(self.lbl_message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, pad_h)

        self._gauge_spacer = sizer.AddSpacer(_SP_MD)
        self.gauge = wx.Gauge(self, range=100, size=(-1, 6))
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, pad_h)

        sizer.AddSpacer(_SP_LG)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        self.btn_cancel = _FlatButton(self, "Cancel", min_width=72)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        row.Add(self.btn_cancel, 0)
        self.Bind(wx.EVT_CLOSE, self._on_close_request)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, pad_h)

        self.SetSizer(sizer)
        self.SetMinSize((380, 120))
        self.Fit()
        self.CentreOnParent()

    def _on_cancel(self, event):
        """Handle cancel button click and notify cancel callback."""
        if self._cancelled:
            return
        self._cancelled = True
        self._message = "Cancelling…"
        self.lbl_message.SetLabel(self._message)
        self.gauge.Pulse()
        self.btn_cancel.Disable()
        self.btn_cancel.SetLabel("Cancelling…")
        self.Layout()
        self.Update()
        if self._on_cancel_requested is not None:
            try:
                self._on_cancel_requested()
            except Exception:
                logger.exception("Cancel request handler failed")

    def _on_close_request(self, event):
        """Escape or the titlebar close button."""
        if self._finished:
            self._on_dismiss(event)
            return
        self._on_cancel(event)
        if event.CanVeto():
            event.Veto()

    def _on_dismiss(self, event):
        """
        OK on a finished export: end the modal loop _start_export is waiting in.

        Destroying the window is the caller's job once ShowModal() has
        returned -- a dialog cannot be deleted from inside its own event loop.
        The non-modal branch exists for tests, which drive this dialog
        directly without ever entering a loop.
        """
        if self.IsModal():
            self.EndModal(wx.ID_OK)
            return
        owner = self.GetParent()
        if owner is not None and getattr(owner, "_export_progress", None) is self:
            owner._export_progress = None
            if hasattr(owner, "btn_export"):
                owner.btn_export.Enable()
        self.Hide()
        self.Destroy()

    def show_result(self, message: str, *, complete: bool = True) -> None:
        """
        Turn the progress dialog into the result dialog.

        The export result used to arrive as a second popup after this one was
        torn down -- two windows for one operation, the first vanishing as the
        second appeared. The run now finishes where it started: same window,
        short outcome line, and Cancel becomes OK.
        """
        self._finished = True
        self._message = message
        formatted = _format_dialog_message(message, max_token=48)
        self.lbl_message.SetLabel(formatted)
        self.lbl_message.Wrap(340)
        if not complete or self._cancelled:
            self.gauge.Hide()
            if hasattr(self, "_gauge_spacer") and self._gauge_spacer is not None:
                self._gauge_spacer.Show(False)
        else:
            self.gauge.SetValue(100)
        self.btn_cancel.SetLabel("OK")
        self.btn_cancel.Enable()
        self.btn_cancel.SetTone(_COLORS["success"] if complete else None)
        self.btn_cancel.Unbind(wx.EVT_BUTTON, handler=self._on_cancel)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_dismiss)
        w, h = self.GetBestSize()
        self.SetSize((max(w, 380), max(h, 120)))
        self.Layout()
        self.Update()
        wx.CallAfter(self.btn_cancel.SetFocus)

    def is_finished(self) -> bool:
        """Return True if the export task completed."""
        return self._finished

    def was_cancelled(self) -> bool:
        """Return True if the user cancelled the export."""
        return self._cancelled

    def update(self, value: int, message: str | None) -> None:
        """
        Advance the dialog for one poll of the export worker.

        The gauge stays determinate: it reports how far the export actually
        is, never an indeterminate "busy" animation. Long-running tasks keep
        it moving by reporting progress within themselves through
        ExportContext.report_progress(), rather than the bar being animated
        to look busy while nothing is known.

        Reports arriving after Cancel are dropped: the worker is unwinding and
        its remaining step messages would otherwise scroll over the
        "Cancelling..." the user is waiting to see resolve.
        """
        if self._cancelled:
            self.gauge.Pulse()
            return
        if message and message != self._message:
            self._message = message
            self.lbl_message.SetLabel(message)
            self.Layout()
        value = max(0, min(100, int(value)))
        if value != self._value:
            self._value = value
            self.gauge.SetValue(value)


# kind -> Material Symbol name (see the "msg_" entries kiforge.TAB_ICON_CDN adds
# for these) and its severity colour (hex, for kiforge.prepare_tab_icon_svg's tint).
# Message icons deliberately go through the same fetch/cache/tint/rasterize
# pipeline as the notebook tab icons rather than being drawn by hand -- one
# icon pipeline for the whole plugin, so a change to sourcing, caching or
# tinting applies everywhere instead of to some icons only.
_MSG_ICON_MATERIAL = {
    "success": "success",
    "error": "error",
    "warning": "warning",
    "cancelled": "cancelled",
    "info": "info",
    "question": "question",
}
_MSG_ICON_SIZE = 24  # on-grid (6 * 4)
# Measure the message text wraps at, and the dialog's width floor. The floor
# sits just under the wrap measure so a short message produces a dialog that
# hugs its content instead of being padded out to a fixed width. Both on-grid.
_MSG_TEXT_WRAP = 360
_MSG_MIN_WIDTH = 240
_msg_icon_bitmap_cache: dict[tuple[str, int, str], wx.Bitmap] = {}


def _load_message_icon_bitmap(kind: str, size: int = _MSG_ICON_SIZE) -> wx.Bitmap | None:
    """Rasterize a cached/CDN Material Symbol for the themed message dialog, tinted per severity."""
    colour = _MSG_ICON_COLORS.get(kind, _MSG_ICON_COLORS["info"])
    cache_key = (kind, size, colour)
    if cache_key in _msg_icon_bitmap_cache:
        cached = _msg_icon_bitmap_cache[cache_key]
        return cached if cached.IsOk() else None

    svg_data = kiforge.fetch_tab_icon_svg(f"msg_{_MSG_ICON_MATERIAL.get(kind, 'info')}")
    if not svg_data:
        _msg_icon_bitmap_cache[cache_key] = wx.Bitmap()
        return None
    try:
        tinted = kiforge.prepare_tab_icon_svg(svg_data, colour)
        bundle = wx.BitmapBundle.FromSVG(tinted, (size, size))
        bitmap = bundle.GetBitmap(wx.Size(size, size))
        _msg_icon_bitmap_cache[cache_key] = bitmap
        return bitmap if bitmap.IsOk() else None
    except Exception as exc:
        logger.warning("Failed to rasterize message icon %s: %s", kind, exc)
        return None


class _KiForgeMessageDialog(wx.Dialog):
    """Themed message dialog following the system appearance, used in place of wx.MessageBox."""

    def __init__(
        self,
        parent,
        message: str,
        title: str,
        kind: str,
        buttons: str,
        btn_labels: tuple[str, ...] | None = None,
    ):
        """Initialise themed message dialog matching active appearance ramp."""
        super().__init__(
            parent,
            title=title,
            style=(wx.DEFAULT_DIALOG_STYLE & ~wx.MINIMIZE_BOX & ~wx.MAXIMIZE_BOX),
        )
        refresh_palette()
        self.SetBackgroundColour(_COLORS["app_bg"])

        outer = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)

        formatted_msg = _format_dialog_message(message, max_token=36)
        msg = wx.StaticText(self, label=formatted_msg)
        msg.SetForegroundColour(_COLORS["text"])
        msg.Wrap(_MSG_TEXT_WRAP)

        multiline = msg.GetBestSize().height > msg.GetCharHeight() * 1.5
        icon_align = wx.ALIGN_TOP if multiline else wx.ALIGN_CENTER_VERTICAL

        icon_bmp = _load_message_icon_bitmap(kind)
        if icon_bmp is not None:
            row.Add(wx.StaticBitmap(self, bitmap=icon_bmp), 0, icon_align | wx.RIGHT, _SP_LG)
        row.Add(msg, 1, icon_align)

        outer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _SP_LG)
        outer.AddSpacer(_SP_XL)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        default_btn = self._add_buttons(btn_row, buttons, btn_labels)
        outer.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _SP_LG)

        self.SetSizer(outer)
        self.SetMinSize((_MSG_MIN_WIDTH, -1))
        self.Fit()
        if parent:
            self.CentreOnParent()
        else:
            self.Centre()
        wx.CallAfter(default_btn.SetFocus)

    def _add_buttons(
        self,
        btn_row: wx.BoxSizer,
        buttons: str,
        btn_labels: tuple[str, ...] | None = None,
    ) -> "_FlatButton":
        """Populate the action button row based on buttons configuration."""
        if buttons == "yes_no":
            label_no = btn_labels[0] if (btn_labels and len(btn_labels) > 0) else "No"
            label_yes = btn_labels[1] if (btn_labels and len(btn_labels) > 1) else "Yes"
            btn_no = _FlatButton(self, label_no, min_width=72)
            btn_no.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_NO))
            btn_row.Add(btn_no, 0, wx.RIGHT, _SP_SM)
            btn_yes = _FlatButton(self, label_yes, primary=True, min_width=72)
            btn_yes.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_YES))
            btn_row.Add(btn_yes, 0)
            self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_NO))
            return btn_yes
        label_ok = btn_labels[0] if (btn_labels and len(btn_labels) > 0) else "OK"
        btn_ok = _FlatButton(self, label_ok, primary=True, min_width=72)
        btn_ok.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_OK))
        btn_row.Add(btn_ok, 0)
        self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_OK))
        return btn_ok


def _message_box(
    message: str,
    caption: str = "KiForge",
    style: int = wx.OK | wx.ICON_INFORMATION,
    parent=None,
    kind: str | None = None,
    btn_labels: tuple[str, ...] | None = None,
) -> int:
    """
    Themed drop-in for ``wx.MessageBox`` matching Studio's dark UI.

    Accepts the same argument order and ``wx.OK`` / ``wx.YES_NO`` / ``wx.ICON_*``
    style flags as ``wx.MessageBox``, and returns the same result codes
    (``wx.ID_OK``, ``wx.ID_YES``, ``wx.ID_NO``). ``wx`` has no built-in icon for
    "success" or "cancelled" specifically, so pass ``kind`` explicitly at call
    sites that want that sharper distinction (e.g. a completed export vs. a
    generic info dialog); it overrides whatever ``style`` would otherwise imply.
    """
    if kind is None:
        if style & wx.ICON_ERROR:
            kind = "error"
        elif style & wx.ICON_WARNING:
            kind = "warning"
        elif style & wx.ICON_QUESTION:
            kind = "question"
        else:
            kind = "info"
    buttons = "yes_no" if (style & wx.YES_NO) else "ok"
    dlg = _KiForgeMessageDialog(parent, message, caption, kind, buttons, btn_labels=btn_labels)
    try:
        return dlg.ShowModal()
    finally:
        dlg.Destroy()


_DIALOG_MIN_WIDTH = 580
_DIALOG_MIN_HEIGHT = 480

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
        "export_homebrew_pdf": True,
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
        "export_homebrew_pdf": False,
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
        "export_homebrew_pdf": True,
        "format_jlc": False,
    },
}
_EXPORT_TOGGLE_KEYS = (
    "export_gerbers", "export_drills", "export_pos", "export_bom", "export_ibom",
    "export_sch_pdf", "export_step", "export_3d", "export_svg", "export_homebrew_pdf",
)

_TAB_ICON_NAMES = ("export", "advanced", "releases")
_TAB_ICON_RASTER_SIZE = 48
_tab_icon_bitmap_cache: dict[tuple[str, int, str], wx.Bitmap] = {}


def _load_tab_icon_bitmap(name: str, size: int = 18, tint: str | None = None) -> wx.Bitmap | None:
    """Rasterize a bundled Material Symbol for notebook tabs, tinted for the theme."""
    if tint is None:
        tint = _TAB_ICON_TINTS[active_palette_mode()]
    cache_key = (name, size, tint)
    if cache_key in _tab_icon_bitmap_cache:
        cached_bmp = _tab_icon_bitmap_cache[cache_key]
        return cached_bmp if cached_bmp.IsOk() else None

    svg_data = kiforge.fetch_tab_icon_svg(name)
    if not svg_data:
        _tab_icon_bitmap_cache[cache_key] = wx.Bitmap()
        return None
    try:
        tinted = kiforge.prepare_tab_icon_svg(svg_data, tint)
        bundle = wx.BitmapBundle.FromSVG(tinted, (_TAB_ICON_RASTER_SIZE, _TAB_ICON_RASTER_SIZE))
        bitmap = bundle.GetBitmap(wx.Size(size, size))
        _tab_icon_bitmap_cache[cache_key] = bitmap
        return bitmap if bitmap.IsOk() else None
    except Exception as exc:
        logger.warning("Failed to rasterize tab icon %s: %s", name, exc)
        return None


def _kicad_parent_window():
    """
    The KiCad frame a Studio dialog should belong to.

    A dialog with no parent is an unowned window, and Cocoa gives unowned
    windows a floating level: it then sits above *every* application, not just
    KiCad -- over the browser, the terminal, everything -- and cannot be sent
    behind them. Owning it to the invoking frame makes it behave like a normal
    document-modal dialog and keeps it inside KiCad's window layer.

    ``wx.GetApp().GetTopWindow()`` is not enough on its own: KiCad runs several
    frames (project manager, PCB editor, schematic editor) and can report one
    that is hidden or not the one the toolbar button was pressed in, which
    leaves the dialog unowned again. Prefer the active window, then any visible
    frame, and only then fall back.
    """
    try:
        active = wx.GetActiveWindow()
        if active is not None:
            top = active.GetTopLevelParent()
            if top is not None and top.IsShown():
                return top
    except Exception:
        pass
    try:
        for win in wx.GetTopLevelWindows():
            if isinstance(win, wx.Frame) and win.IsShown():
                return win
    except Exception:
        pass
    app = wx.GetApp()
    return app.GetTopWindow() if app else None


def _destroy_progress_dialog(progress):
    """
    Take the export progress dialog down from outside.

    It runs its own modal loop, so it is *ended* rather than destroyed here:
    deleting a window from inside the event loop it is running leaves wx
    driving a loop for a dead object. The Destroy() belongs to whoever called
    ShowModal(), once that call returns.
    """
    if not progress:
        return
    try:
        if progress.IsModal():
            progress.EndModal(wx.ID_CANCEL)
            return
    except Exception:
        pass
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
    
    def __init__(self, parent, project_dir=None, pcb_file=None):
        """
        Initialize the KiForge Studio configuration dialog.

        Args:
            parent: Parent wx.Window or None when running in standalone mode.
            project_dir (str, optional): Pre-resolved project directory or board file path.
            pcb_file (str, optional): Path to active .kicad_pcb board file.
        """
        super(KiForgeStudioSettingsDialog, self).__init__(
            parent, 
            title="KiForge",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX
        )
        # Resolve light/dark before a single widget is built: every custom paint
        # handler reads _COLORS, and the window chrome around them is drawn by
        # the OS in the system appearance regardless.
        refresh_palette()
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._on_system_colour_changed)
        self.project_dir = project_dir
        self.pcb_file = os.path.abspath(pcb_file) if pcb_file else None
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
        self._export_summary_text = ""
        self._export_running = False
        self._export_close_after_finish = False
        self._applying_preset = False
        self._settings_project_dir = project_dir
        # True while settings are being programmatically applied to controls
        # (construction, Load Global Config, Reset). wx.TextCtrl.SetValue()
        # genuinely fires EVT_TEXT (unlike checkboxes/choices, which don't fire
        # their change events from SetValue()), so populating the dialog can
        # otherwise trigger the live-CD-sync handlers mid-populate and have them
        # read half-initialized controls back into self.settings. This flag is
        # the actual invariant that prevents that class of bug regardless of
        # which control triggers it or what order settings get applied in --
        # see on_export_setting_changed()/on_export_checkbox_changed().
        self._initializing = True

        self.init_ui()
        self.update_ui_from_settings()
        self._initializing = False
        self._fit_dialog_to_screen()
        self.Center()
        self._bind_keyboard_shortcuts()

        self._export_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_export_progress, self._export_timer)
        self.Bind(wx.EVT_CLOSE, self.on_window_close)
        self._check_dependencies_async()
        wx.CallAfter(self._set_initial_focus)

    def _set_initial_focus(self):
        """Place initial focus on the export action button or container to prevent highlighting the first checkbox."""
        try:
            if hasattr(self, "btn_export") and self.btn_export.IsEnabled():
                self.btn_export.SetFocus()
            elif hasattr(self, "notebook") and self.notebook:
                self.notebook.SetFocus()
        except Exception:
            pass

    def _check_dependencies_async(self):
        """Check and install missing PDF renderer dependencies (Pillow) in the background."""
        def worker():
            """Background worker thread for checking and installing missing PDF renderers."""
            try:
                target_python = kiforge.PathResolver.get_kicad_python_path()
                if not target_python or not os.path.isfile(target_python):
                    return
                missing = kiforge.missing_pdf_renderer_packages(python_exe=target_python)
                if not missing:
                    logger.debug("PDF renderer requirements (Pillow) already satisfied.")
                    return
                logger.info("Background installing missing PDF renderer packages: %s...", missing)
                ok, msg = kiforge.install_pdf_renderer(missing, python_exe=target_python, log=logger)
                if ok:
                    logger.info("Background installation of Pillow succeeded.")
                else:
                    logger.warning("Background installation of Pillow failed: %s", msg)
            except Exception as exc:
                logger.debug("Error in background dependency check: %s", exc)

        threading.Thread(target=worker, daemon=True, name="kiforge-dep-check").start()

    def on_window_close(self, event):
        """Handle title-bar close while an export may still be running."""
        if self._export_running:
            if _message_box(
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
            self._export_close_after_finish = True
            self._finish_export_progress()
        event.Skip()

    def init_ui(self):
        """Assemble Studio header, tabbed settings notebook, and footer controls."""
        self.SetBackgroundColour(_COLORS["app_bg"])
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self._build_header_panel(), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _SP_LG)
        main_sizer.Add(self._separator(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _SP_LG)
        main_sizer.AddSpacer(_SP_SM)
        self.notebook = wx.Notebook(self, style=wx.BK_DEFAULT)
        self.notebook.SetBackgroundColour(_COLORS["app_bg"])
        try:
            self.notebook.SetForegroundColour(_COLORS["text"])
        except Exception:
            pass
        self._init_notebook_image_list()
        self._build_export_tab()
        self._build_advanced_tab()
        self._build_releases_tab()
        self._apply_notebook_icons()
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, _SP_LG)
        main_sizer.AddSpacer(_SP_LG)
        main_sizer.Add(self._build_footer_panel(), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _SP_LG)

        self.SetSizer(main_sizer)
        self.SetMinSize((_DIALOG_MIN_WIDTH, _DIALOG_MIN_HEIGHT))

        self.Bind(wx.EVT_SIZE, self._on_dialog_resize)
        self.Bind(wx.EVT_SIZING, self._on_dialog_sizing)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_notebook_page_changed)

    def _on_notebook_page_changed(self, event=None):
        """Ensure the newly selected notebook page recalculates its layout."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        if hasattr(self, "notebook") and self.notebook:
            page = self.notebook.GetCurrentPage()
            if page:
                page.Layout()

    def _on_system_colour_changed(self, event):
        """Re-resolve the palette and repaint when the OS/KiCad theme flips live."""
        self.apply_theme()
        event.Skip()

    def apply_theme(self):
        """Recursively update background/foreground colors and redraw the dialog tree."""
        refresh_palette(self.GetParent())
        self.SetBackgroundColour(_COLORS["app_bg"])
        if hasattr(self, "notebook") and self.notebook:
            self.notebook.SetBackgroundColour(_COLORS["app_bg"])
            try:
                self.notebook.SetForegroundColour(_COLORS["text"])
            except Exception:
                pass
        if hasattr(self, "footer") and self.footer:
            self.footer.SetBackgroundColour(_COLORS["app_bg"])
        self._apply_theme_to_tree(self)
        self._apply_notebook_icons()
        self.Refresh()
        self.Update()

    def _apply_theme_to_tree(self, window: wx.Window):
        """Recursively propagate active palette colors to all child controls."""
        if not window:
            return
        for child in window.GetChildren():
            if isinstance(child, wx.TextCtrl):
                self._style_input(child)
            elif isinstance(child, wx.StaticText):
                fg = child.GetForegroundColour()
                is_muted = fg in (_DARK_PALETTE["muted"], _LIGHT_PALETTE["muted"])
                child.SetForegroundColour(_COLORS["muted"] if is_muted else _COLORS["text"])
            elif isinstance(child, (_FlatChoice, _FlatButton, _FlatCheckBox, _FlatRadioButton)):
                child.SetBackgroundColour(_hal_resolve_bg(child))
                child.Refresh()
            elif isinstance(child, (wx.ScrolledWindow, wx.Panel)):
                bg = child.GetBackgroundColour()
                if bg in (_DARK_PALETTE["surface"], _LIGHT_PALETTE["surface"]):
                    child.SetBackgroundColour(_COLORS["surface"])
                elif bg in (_DARK_PALETTE["footer_bg"], _LIGHT_PALETTE["footer_bg"]):
                    child.SetBackgroundColour(_COLORS["footer_bg"])
                else:
                    child.SetBackgroundColour(_COLORS["app_bg"])
                self._apply_theme_to_tree(child)
                child.Refresh()
            else:
                self._apply_theme_to_tree(child)
                child.Refresh()

    def _init_notebook_image_list(self):
        """Pre-allocate notebook ImageList with tab icons before adding pages for GTK safety."""
        display_size = 18
        bitmaps = [_load_tab_icon_bitmap(name, display_size) for name in _TAB_ICON_NAMES]
        if all(bmp and bmp.IsOk() for bmp in bitmaps):
            image_list = wx.ImageList(display_size, display_size)
            for bmp in bitmaps:
                image_list.Add(bmp)
            self.notebook.AssignImageList(image_list)

    def _attach_tab_icons(self):
        """Configure notebook tabs for native desktop display."""
        self._apply_notebook_icons()

    def _apply_notebook_icons(self):
        """Update tab icon bitmaps in the existing ImageList on theme changes."""
        image_list = self.notebook.GetImageList()
        if image_list:
            display_size = 18
            bitmaps = [_load_tab_icon_bitmap(name, display_size) for name in _TAB_ICON_NAMES]
            for index, bmp in enumerate(bitmaps):
                if bmp and bmp.IsOk() and index < image_list.GetImageCount():
                    image_list.Replace(index, bmp)
                if index < self.notebook.GetPageCount():
                    self.notebook.SetPageImage(index, index)

    def _bind_keyboard_shortcuts(self):
        """Bind Ctrl+1/2/3 tab switching and Enter export shortcut."""
        def on_char_hook(event):
            """Hook keyboard events for Ctrl+1/2/3 tab switching and Enter export."""
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
        """Create a 1px horizontal separator line matching border palette tone."""
        line = wx.Panel(parent, size=(-1, 1))
        line.SetBackgroundColour(_COLORS["border"])
        line.SetMinSize((-1, 1))
        return line

    def _style_panel(self, panel: wx.Panel, *, surface: bool = True) -> None:
        """Apply palette background colour and background click focus clearing to panel."""
        panel.SetBackgroundColour(_COLORS["surface"] if surface else _COLORS["app_bg"])
        self._clear_focus_on_background_click(panel)

    def _clear_focus_on_background_click(
        self, event_source: wx.Window, focus_target: wx.Window | None = None
    ) -> None:
        """
        Move focus off a custom control when the user clicks dead background.

        A native control loses its focus highlight when you click elsewhere,
        because whatever you clicked takes focus. _FlatCheckBox and
        _FlatRadioButton paint themselves rather than wrapping a native widget,
        and blank panel background claims no focus at all, so without this the
        accent highlight stays lit until something else explicitly steals it.

        Handing focus to the containing panel is the whole mechanism, and it is
        the same call on every platform. Wired through _style_panel,
        _section_label and _muted_label -- every non-interactive surface in the
        dialog goes through one of those -- so it applies uniformly instead of
        being re-solved per tab.
        """
        target = focus_target or event_source

        def _on_click(event):
            """Clear focus from custom painted control on background panel click."""
            target.SetFocusIgnoringChildren()
            event.Skip()

        event_source.Bind(wx.EVT_LEFT_DOWN, _on_click)

    def _style_text(self, label: wx.StaticText, *, muted: bool = False) -> wx.StaticText:
        """Apply palette foreground colour to static text label."""
        label.SetForegroundColour(_COLORS["muted"] if muted else _COLORS["text"])
        return label

    def _style_input(self, ctrl: wx.TextCtrl) -> wx.TextCtrl:
        """Apply palette colours and caret styling to text input control."""
        ctrl.SetBackgroundColour(_COLORS["input_bg"])
        ctrl.SetForegroundColour(_COLORS["input_fg"])
        try:
            ctrl.SetInsertionPointEnd()
        except Exception:
            pass
        return ctrl

    def _section_label(self, parent, text: str) -> wx.StaticText:
        """Construct a section header label with standard muted styling."""
        lbl = wx.StaticText(parent, label=text)
        lbl.SetForegroundColour(_COLORS["muted"])
        font = lbl.GetFont()
        font.SetWeight(wx.FONTWEIGHT_NORMAL)
        lbl.SetFont(font)
        self._clear_focus_on_background_click(lbl, parent)
        return lbl

    def _muted_label(self, parent, text: str, wrap: int | None = None) -> wx.StaticText:
        """Construct a muted description label with optional wrapping."""
        lbl = wx.StaticText(parent, label=text)
        self._style_text(lbl, muted=True)
        if wrap:
            lbl.Wrap(wrap)
        self._clear_focus_on_background_click(lbl, parent)
        return lbl

    def _build_header_panel(self):
        """Build the top dialog banner containing the brand glyph and title."""
        banner = wx.Panel(self)
        self._style_panel(banner, surface=False)
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        accent = wx.Panel(banner, size=(_SP_XS, _SP_XXL))
        accent.SetBackgroundColour(_COLORS["accent"])
        accent.SetMinSize((_SP_XS, _SP_XXL))
        title = wx.StaticText(banner, label="KiForge")
        title.SetForegroundColour(_COLORS["text"])
        font = title.GetFont()
        font.SetPointSize(13)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        self._clear_focus_on_background_click(title, banner)
        sizer.Add(accent, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _SP_MD)
        sizer.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        banner.SetSizer(sizer)
        return banner

    def _build_export_tab(self):
        """Build the primary Outputs tab with export checkboxes and presets."""
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        scroll = wx.ScrolledWindow(page, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 10)
        self._style_panel(scroll, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT

        sizer.AddSpacer(_SP_SM)
        sizer.Add(self._section_label(scroll, "Project"), 0, inset, _SP_SM)
        sizer.AddSpacer(_SP_XS)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_project_dir = wx.TextCtrl(scroll)
        self._style_input(self.txt_project_dir)
        if self.project_dir:
            self.txt_project_dir.SetValue(self.project_dir)
        self.txt_project_dir.Bind(wx.EVT_KILL_FOCUS, self.on_project_dir_changed)
        btn_browse = _FlatButton(scroll, "Browse", min_width=72)
        btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        row.Add(self.txt_project_dir, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _SP_SM)
        row.Add(btn_browse, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | inset, _SP_SM)
        sizer.AddSpacer(_SP_MD)

        sizer.Add(self._section_label(scroll, "Output folder"), 0, inset, _SP_SM)
        sizer.AddSpacer(_SP_XS)
        self.txt_output_dir = wx.TextCtrl(scroll)
        self._style_input(self.txt_output_dir)
        self.txt_output_dir.SetValue(self.settings.get("output_dir", "kiforge"))
        sizer.Add(self.txt_output_dir, 0, wx.EXPAND | inset, _SP_SM)
        sizer.AddSpacer(_SP_LG)

        sizer.Add(self._section_label(scroll, "Preset"), 0, inset, _SP_SM)
        sizer.AddSpacer(_SP_SM)
        self._preset_radios = []
        for label in EXPORT_PRESET_RADIO_LABELS:
            # Appends itself to self._preset_radios and joins that group for
            # mutual exclusivity -- see _FlatRadioButton's group parameter.
            rb = _FlatRadioButton(scroll, label=label, group=self._preset_radios)
            rb.Bind(wx.EVT_RADIOBUTTON, self.on_preset_changed)
            sizer.Add(rb, 0, inset, _SP_SM)
            sizer.AddSpacer(_SP_XS)

        sizer.AddSpacer(_SP_XS)
        self.lbl_export_summary = wx.StaticText(scroll, label="")
        # Never let this label dictate the layout's width: a StaticText reports
        # its full unwrapped text as its minimum size, and the summary is long.
        # Left alone it sets the scrolled panel's virtual width far wider than
        # the dialog, and everything beside it -- the Browse button, the output
        # folder field -- gets laid out off the visible area and clipped.
        self.lbl_export_summary.SetMinSize((_SP_SM, -1))
        self._style_text(self.lbl_export_summary, muted=True)
        self._clear_focus_on_background_click(self.lbl_export_summary, scroll)
        # EXPAND so it fills the column: the small min size above stops it
        # dictating the layout's width, but without EXPAND the sizer would
        # then hand it exactly that min size and the text would render into
        # a few pixels.
        sizer.Add(self.lbl_export_summary, 0, wx.EXPAND | inset, _SP_SM)
        sizer.AddSpacer(_SP_LG)

        scroll.SetSizer(sizer)
        scroll.FitInside()
        page.SetSizer(wx.BoxSizer(wx.VERTICAL))
        page.GetSizer().Add(scroll, 1, wx.EXPAND)
        self.notebook.AddPage(page, "Export", imageId=0)

    def _build_advanced_tab(self):
        """Build the Advanced tab containing positioning and 3D model settings."""
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        scroll = wx.ScrolledWindow(page, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 10)
        self._style_panel(scroll, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT

        sizer.Add(self._section_label(scroll, "Outputs"), 0, inset | wx.TOP, _SP_SM)
        columns = wx.BoxSizer(wx.HORIZONTAL)

        mfg_col = wx.BoxSizer(wx.VERTICAL)
        mfg_col.Add(self._muted_label(scroll, "Manufacturing"), 0, wx.BOTTOM, _SP_SM)
        self.chk_gerbers = _FlatCheckBox(scroll, label="Gerbers")
        self.chk_drills = _FlatCheckBox(scroll, label="Drill files")
        self.chk_pos = _FlatCheckBox(scroll, label="Placement")
        self.chk_bom = _FlatCheckBox(scroll, label="BOM")
        self.chk_ibom = _FlatCheckBox(scroll, label="Interactive BOM")
        self.chk_gerbers.Bind(wx.EVT_CHECKBOX, self.on_gerbers_toggled)
        for chk in (self.chk_drills, self.chk_pos, self.chk_bom, self.chk_ibom):
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        for chk in (self.chk_gerbers, self.chk_drills, self.chk_pos, self.chk_bom, self.chk_ibom):
            mfg_col.Add(chk, 0, wx.TOP, _SP_XS)

        mfg_col.AddSpacer(_SP_SM)
        side_row = wx.BoxSizer(wx.HORIZONTAL)
        side_row.Add(self._muted_label(scroll, "Placement side"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _SP_SM)
        self.choice_pos_side = _FlatChoice(scroll, choices=["Both", "Front", "Back"])
        side_row.Add(self.choice_pos_side, 1, wx.EXPAND)
        mfg_col.Add(side_row, 0, wx.EXPAND | wx.TOP, _SP_XS)
        self.chk_pos_smd_only = _FlatCheckBox(scroll, label="SMD only")
        self.chk_pos_exclude_dnp = _FlatCheckBox(scroll, label="Exclude DNP")
        for chk in (self.chk_pos_smd_only, self.chk_pos_exclude_dnp):
            mfg_col.Add(chk, 0, wx.TOP, _SP_XS)
            chk.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)
        self.choice_pos_side.Bind(wx.EVT_CHOICE, self.on_export_setting_changed)

        mfg_col.AddSpacer(_SP_SM)
        mfg_col.Add(self._muted_label(scroll, "BOM columns"), 0, wx.BOTTOM, _SP_SM)
        # "&&" escapes a literal ampersand -- wx treats a single "&" as a mnemonic
        # marker (underlines the next character), which mangled this label.
        self.chk_bom_mfr_mpn = _FlatCheckBox(scroll, label="Include Manufacturer & MPN")
        mfg_col.Add(self.chk_bom_mfr_mpn, 0, wx.TOP, _SP_XS)
        self.chk_bom_mfr_mpn.Bind(wx.EVT_CHECKBOX, self.on_export_setting_changed)

        doc_col = wx.BoxSizer(wx.VERTICAL)
        doc_col.Add(self._muted_label(scroll, "Documentation"), 0, wx.BOTTOM, _SP_SM)
        self.chk_sch_pdf = _FlatCheckBox(scroll, label="Schematic PDF")
        self.chk_step = _FlatCheckBox(scroll, label="STEP")
        self.chk_3d = _FlatCheckBox(scroll, label="3D renders")
        self.chk_svg = _FlatCheckBox(scroll, label="Copper SVG")
        self.chk_homebrew_pdf = _FlatCheckBox(scroll, label="Homebrew PDF")
        for chk in (self.chk_sch_pdf, self.chk_step, self.chk_3d, self.chk_svg, self.chk_homebrew_pdf):
            doc_col.Add(chk, 0, wx.TOP, _SP_XS)
        self.chk_sch_pdf.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        self.chk_step.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        self.chk_3d.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        self.chk_svg.Bind(wx.EVT_CHECKBOX, self.on_export_checkbox_changed)
        self.chk_homebrew_pdf.Bind(wx.EVT_CHECKBOX, self.on_homebrew_pdf_toggled)

        columns.Add(mfg_col, 1, wx.EXPAND | wx.RIGHT, _SP_MD)
        columns.Add(doc_col, 1, wx.EXPAND)
        sizer.Add(columns, 0, wx.EXPAND | inset | wx.TOP, _SP_SM)
        sizer.AddSpacer(_SP_LG)


        scroll.SetSizer(sizer)
        scroll.FitInside()
        page.SetSizer(wx.BoxSizer(wx.VERTICAL))
        page.GetSizer().Add(scroll, 1, wx.EXPAND)
        self.notebook.AddPage(page, "Advanced", imageId=1)

    def _build_releases_tab(self):
        """Build the Continuous Delivery tab with workflow options."""
        page = wx.Panel(self.notebook)
        self._cd_page = page
        self._style_panel(page, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT | wx.TOP

        sizer.Add(self._section_label(page, "Continuous Delivery"), 0, inset, _SP_MD)

        self.lbl_cd_sync_status = wx.StaticText(page, label="")
        self._style_text(self.lbl_cd_sync_status)
        self._clear_focus_on_background_click(self.lbl_cd_sync_status, page)
        sizer.Add(self.lbl_cd_sync_status, 0, inset | wx.TOP, _SP_SM)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_generate_cd = _FlatButton(page, "Set up workflows", primary=True, min_width=160)
        self.btn_generate_cd.Bind(wx.EVT_BUTTON, self.on_generate_cd)
        btn_row.Add(self.btn_generate_cd, 0, wx.RIGHT, _SP_SM)

        self.btn_unlock_cd = _FlatButton(page, "", min_width=32, icon_kind="lock")
        self.btn_unlock_cd.Bind(wx.EVT_BUTTON, self.on_unlock_cd_workflows)
        btn_row.Add(self.btn_unlock_cd, 0)
        sizer.Add(btn_row, 0, inset | wx.TOP, _SP_MD)
        sizer.AddSpacer(_SP_LG)

        page.SetSizer(sizer)
        self.notebook.AddPage(page, "Releases", imageId=2)
        self._refresh_cd_workflow_status()

    def _build_footer_panel(self):
        """Build the bottom action bar with summary text and export button."""
        footer = wx.Panel(self)
        self.footer = footer
        footer.SetBackgroundColour(_COLORS["app_bg"])
        self._clear_focus_on_background_click(footer)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_save = _FlatButton(footer, "Save", min_width=64)
        btn_save.Bind(wx.EVT_BUTTON, self.on_settings_menu)

        self.lbl_status_toast = wx.StaticText(footer, label="")
        self.lbl_status_toast.SetForegroundColour(_COLORS["text"])
        self._clear_focus_on_background_click(self.lbl_status_toast, footer)

        self.btn_export = _FlatButton(footer, "Export", primary=True, min_width=72)
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_run_export)

        btn_close = _FlatButton(footer, "Close", min_width=64)
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)

        sizer.Add(btn_save, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.AddSpacer(_SP_MD)
        sizer.Add(self.lbl_status_toast, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.AddStretchSpacer()
        sizer.Add(self.btn_export, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, _SP_SM)
        sizer.Add(btn_close, 0, wx.ALIGN_CENTER_VERTICAL)
        footer.SetSizer(sizer)
        return footer

    def _refresh_scroll_layout(self):
        """Refresh scrolled window layout to adjust to container dimension changes."""
        self.Layout()

    def on_settings_menu(self, event):
        """Display popup menu for loading, saving, or resetting configuration."""
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
        """Return the index of the currently active preset or -1 for custom."""
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
        if self._initializing:
            return
        if not self._applying_preset:
            self._set_preset_choice("custom")
        self._update_export_summary()
        self.on_export_setting_changed(event)

    def _apply_export_preset(self, preset_id: str):
        """Apply preset output settings to checkboxes and update dependant UI controls."""
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
            "export_homebrew_pdf": self.chk_homebrew_pdf,
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
            self._sync_file_availability_state()
            self._update_export_summary()
        finally:
            self._applying_preset = False

    def _set_preset_choice(self, preset_id: str):
        """Select the corresponding preset radio button."""
        labels = [pid for pid, _ in EXPORT_PRESET_CHOICES]
        if preset_id in labels:
            idx = labels.index(preset_id)
            if 0 <= idx < len(self._preset_radios):
                self._preset_radios[idx].SetValue(True)

    def _detect_active_preset(self) -> str:
        """Identify preset matching active export checkbox combination, or return 'custom'."""
        current = {key: getattr(self, self._export_checkbox_attr(key)).IsChecked() for key in _EXPORT_TOGGLE_KEYS}
        current["format_jlc"] = self._export_setting("format_jlc")
        for preset_id, values in EXPORT_PRESETS.items():
            if all(current.get(key) == value for key, value in values.items()):
                return preset_id
        return "custom"

    @staticmethod
    def _export_checkbox_attr(export_key: str) -> str:
        """Return the checkbox attribute name corresponding to the export setting key."""
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
            "export_homebrew_pdf": "chk_homebrew_pdf",
        }
        return mapping[export_key]

    def _update_export_summary(self):
        """Update footer summary text to reflect enabled outputs."""
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
            "export_homebrew_pdf": "Homebrew PDF",
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
        self._export_summary_text = summary
        self._apply_export_summary()
        self._sync_export_button_state(bool(enabled))

    def _apply_export_summary(self):
        """
        Re-wrap the export summary to the width actually available to it.

        wx.StaticText.Wrap() rewrites the control's own label, inserting the
        line breaks into it, so it has to be applied to the original text
        every time: wrapping whatever the label currently holds means each
        resize re-wraps already-wrapped text and the breaks compound.

        The width comes from the label's real parent (the scrolled panel,
        whose client width already excludes a scrollbar when one is shown)
        rather than from the dialog, which is wider than the space this label
        actually gets -- measuring against it wrapped too late and left the
        last entry running past the edge.
        """
        label = getattr(self, "lbl_export_summary", None)
        if label is None:
            return
        label.SetLabel(self._export_summary_text)
        parent = label.GetParent()
        if parent is None:
            return
        available = parent.GetClientSize().width - (_SP_SM * 2)
        if available > 0:
            label.Wrap(available)
        # Wrapping changes the label's height, so the column must re-lay out
        # or the controls after it keep the old spacing.
        parent.Layout()

    def _on_dialog_resize(self, event):
        """Adjust dialog layout and text wrapping on window resize."""
        self._apply_export_summary()
        if event is not None:
            event.Skip()

    def _on_dialog_sizing(self, event):
        """
        Snap an interactive resize onto the same 4pt grid the layout uses.

        EVT_SIZING, not EVT_SIZE: this event carries the *proposed* rectangle
        and lets it be adjusted before wx applies it, so the window is never
        painted at an off-grid size and there is no resize-triggers-resize
        feedback loop (which is what adjusting inside EVT_SIZE would cause).
        """
        rect = event.GetRect()
        width, height = _snap_to_grid(rect.width), _snap_to_grid(rect.height)
        if (width, height) != (rect.width, rect.height):
            rect.width, rect.height = width, height
            event.SetRect(rect)
        event.Skip()

    def _natural_height(self) -> int:
        """
        Height at which the tallest tab shows all of its content at once.

        The tabs stay scrollable so a small screen, or a window the user has
        deliberately shrunk, still works -- but opening already scrolled, with
        a scrollbar sitting beside content that would have fitted, just looks
        broken. Derived from the content rather than assumed, so adding a
        control to a tab cannot silently reintroduce an opening scrollbar.

        Falls back to the minimum height if measured before layout, when the
        sizers have nothing meaningful to report yet.
        """
        content = 0
        for index in range(self.notebook.GetPageCount()):
            page = self.notebook.GetPage(index)
            for child in page.GetChildren():
                sizer = child.GetSizer()
                if sizer is not None:
                    content = max(content, sizer.GetMinSize().height)
        if content <= 0:
            return _DIALOG_MIN_HEIGHT
        # Everything that is not the page's own client area: title bar, header
        # banner, tab strip, footer and the window borders.
        page_height = self.notebook.GetPage(0).GetClientSize().height
        chrome = max(0, self.GetSize().height - page_height)
        return content + chrome + _SP_XS

    def _fit_dialog_to_screen(self):
        """Constrain dialog dimensions to fit comfortably within display bounds."""
        try:
            display_w, display_h = wx.DisplaySize()
        except Exception:
            display_w, display_h = 1024, 768
        # Lay out first so the sizers can report real content sizes.
        self.Layout()
        # Snapped: the display can be any size, so deriving from it can land
        # off-grid even though the constants either side of it do not.
        width = _snap_to_grid(min(720, max(_DIALOG_MIN_WIDTH, min(640, display_w - 80))))
        natural_h = self._natural_height()
        target_h = max(_DIALOG_MIN_HEIGHT, min(natural_h, 540))
        height = _snap_to_grid(
            min(max(_DIALOG_MIN_HEIGHT, display_h - 100), target_h)
        )
        self.SetSize((width, height))
        self._on_dialog_resize(None)
        self.Layout()

    def on_export_setting_changed(self, event=None):
        """Update export parameters when toggles change."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        if self._initializing:
            return
        self.settings["export_params"] = self._collect_export_params()

    def _has_existing_cd_workflows(self) -> bool:
        """Return True if release workflow files exist in the project directory."""
        project_dir = self.txt_project_dir.GetValue().strip() if hasattr(self, "txt_project_dir") else ""
        if not project_dir or not os.path.isdir(project_dir):
            return False
        gh_yml = os.path.join(project_dir, ".github", "workflows", "release.yml")
        gt_yml = os.path.join(project_dir, ".gitea", "workflows", "release.yml")
        return os.path.isfile(gh_yml) or os.path.isfile(gt_yml)

    def _refresh_cd_workflow_status(self):
        """Update Releases tab UI state based on whether workflows exist."""
        if not hasattr(self, "lbl_cd_sync_status") or not self.lbl_cd_sync_status:
            return
        exists = self._has_existing_cd_workflows()
        self._cd_unlocked = getattr(self, "_cd_unlocked", False)
        if exists:
            if hasattr(self, "btn_generate_cd"):
                self.btn_generate_cd.SetLabel("Overwrite workflows")
            if self._cd_unlocked:
                self.lbl_cd_sync_status.SetLabel("Workflows configured [Unlocked]")
                self.lbl_cd_sync_status.SetForegroundColour(_COLORS["text"])
                if hasattr(self, "btn_generate_cd"):
                    self.btn_generate_cd.Enable(True)
                if hasattr(self, "btn_unlock_cd"):
                    self.btn_unlock_cd.SetIcon("unlock")
                    self.btn_unlock_cd.Show(True)
            else:
                self.lbl_cd_sync_status.SetLabel("Workflows configured [Locked]")
                self.lbl_cd_sync_status.SetForegroundColour(_COLORS["text"])
                if hasattr(self, "btn_generate_cd"):
                    self.btn_generate_cd.Enable(False)
                if hasattr(self, "btn_unlock_cd"):
                    self.btn_unlock_cd.SetIcon("lock")
                    self.btn_unlock_cd.Show(True)
        else:
            self.lbl_cd_sync_status.SetLabel("No release workflows configured for this project.")
            self.lbl_cd_sync_status.SetForegroundColour(_COLORS["muted"])
            if hasattr(self, "btn_generate_cd"):
                self.btn_generate_cd.SetLabel("Set up workflows")
                self.btn_generate_cd.Enable(True)
            if hasattr(self, "btn_unlock_cd"):
                self.btn_unlock_cd.Show(False)
        if hasattr(self, "_cd_page") and self._cd_page:
            self._cd_page.Layout()
        if hasattr(self, "notebook") and self.notebook:
            self.notebook.Layout()

    def on_unlock_cd_workflows(self, event=None):
        """Toggle the locked/unlocked state of CD workflows."""
        self._cd_unlocked = not getattr(self, "_cd_unlocked", False)
        self._refresh_cd_workflow_status()

    def _show_status_message(self, message: str, is_error: bool = False):
        """Display non-modal status update in the footer with auto-clearing timer."""
        if not hasattr(self, "lbl_status_toast") or not self.lbl_status_toast:
            return
        colour = _COLORS["error"] if is_error else _COLORS["accent"]
        self.lbl_status_toast.SetForegroundColour(colour)
        self.lbl_status_toast.SetLabel(message)
        if hasattr(self, "footer") and self.footer:
            self.footer.Layout()
        if not hasattr(self, "_status_toast_timer") or self._status_toast_timer is None:
            self._status_toast_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_status_toast_timer, self._status_toast_timer)
        self._status_toast_timer.Start(3500, oneShot=True)

    def _on_status_toast_timer(self, event=None):
        """Clear the status toast label."""
        if hasattr(self, "lbl_status_toast") and self.lbl_status_toast:
            self.lbl_status_toast.SetLabel("")
            if hasattr(self, "footer") and self.footer:
                self.footer.Layout()

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
        # Populate the Advanced-tab controls (pos_side, SMD only, ...) first.
        # self.txt_output_dir.SetValue() below fires EVT_TEXT (unlike every
        # other SetValue() call in this method, wx.TextCtrl genuinely does
        # generate the event programmatically), which on_export_setting_changed
        # handles by reading those controls back via _collect_export_params()
        # and overwriting self.settings["export_params"] with whatever they
        # currently hold. If that fires before this line runs, it captures the
        # controls' just-constructed defaults ("both"/unchecked) and clobbers
        # the real loaded values before the user ever sees them restored.
        self._apply_export_params_to_ui()

        self.chk_gerbers.SetValue(self._export_setting('export_gerbers'))
        self.chk_drills.SetValue(self._export_setting('export_drills'))
        self.chk_pos.SetValue(self._export_setting('export_pos'))
        self.chk_bom.SetValue(self._export_setting('export_bom'))
        self.chk_ibom.SetValue(self._export_setting('export_ibom'))
        self.chk_sch_pdf.SetValue(self._export_setting('export_sch_pdf'))
        self.chk_step.SetValue(self._export_setting('export_step'))
        self.chk_3d.SetValue(self._export_setting('export_3d'))
        self.chk_svg.SetValue(self._export_setting('export_svg'))
        self.chk_homebrew_pdf.SetValue(self._export_setting('export_homebrew_pdf'))
        self.txt_output_dir.SetValue(self.settings.get('output_dir', 'kiforge'))
        self._set_preset_choice(self._detect_active_preset())
        self._sync_drill_checkbox_state()
        self._sync_svg_pdf_checkbox_state()
        self._sync_file_availability_state()
        self._update_export_summary()

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
        """Populate Advanced tab input widgets from active export parameters."""
        side_map = {"both": 0, "front": 1, "back": 2}
        self.choice_pos_side.SetSelection(side_map.get(self._export_param("pos_side", "both"), 0))
        self.chk_pos_smd_only.SetValue(bool(self._export_param("pos_smd_only", True)))
        self.chk_pos_exclude_dnp.SetValue(bool(self._export_param("pos_exclude_dnp", True)))
        self.chk_bom_mfr_mpn.SetValue(bool(self._export_param("bom_include_mfr_mpn", True)))

    def _reload_settings(self, project_dir=None):
        """Reload merged settings into the dialog (global + project when dir is set)."""
        self.settings = kiforge.load_merged_settings(project_dir)
        self._initializing = True
        try:
            self.update_ui_from_settings()
        finally:
            self._initializing = False

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
            'export_homebrew_pdf': self.chk_homebrew_pdf.IsChecked(),
            'format_jlc': self._export_setting('format_jlc'),
        }
        return {
            'output_dir': self.txt_output_dir.GetValue().strip(),
            **exports,
            'exports': exports,
            'export_params': self._collect_export_params(),
        }

    def _sync_drill_checkbox_state(self):
        """
        Synchronize drill export state with Gerber manufacturing requirements.

        Because Gerber production archives bundle drill and NC drill files
        together, drill export is engaged and locked whenever Gerber export
        is selected.
        """
        if self.chk_gerbers.IsChecked():
            self.chk_drills.SetValue(True)
            self.chk_drills.Disable()
        else:
            self.chk_drills.Enable()

    def _sync_svg_pdf_checkbox_state(self):
        """
        Synchronize Copper SVG export state with Homebrew PDF requirements.

        Homebrew etching and mask PDF generation rasterizes directly from
        copper layer SVGs, requiring SVG export to remain active while Homebrew
        PDF export is selected.
        """
        if self.chk_homebrew_pdf.IsChecked():
            self.chk_svg.SetValue(True)
            self.chk_svg.Disable()
        else:
            self.chk_svg.Enable()

    def _resolve_project_source_files(self) -> tuple[str | None, str | None]:
        """
        Locate the active printed circuit board and schematic source files.

        Resolves the primary board path from an explicitly provided board target,
        a direct board path supplied as the project directory, or by scanning
        the project directory for a top-level board file (.kicad_pcb).

        When a board file is identified, schematic lookup is strictly isolated
        to the containing directory to preserve project revisions and maintain
        design coherence.

        Returns:
            tuple[str | None, str | None]: Absolute paths to (pcb_file, schematic_file).
        """
        pcb_file = None
        sch_file = None

        if self.pcb_file and os.path.isfile(self.pcb_file):
            pcb_file = os.path.abspath(self.pcb_file)
        elif self.project_dir and os.path.isfile(self.project_dir) and self.project_dir.endswith(".kicad_pcb"):
            pcb_file = os.path.abspath(self.project_dir)
        elif self.project_dir and os.path.isdir(self.project_dir):
            try:
                pcb_candidates = [
                    os.path.join(self.project_dir, f)
                    for f in os.listdir(self.project_dir)
                    if f.endswith(".kicad_pcb") and not f.startswith(".") and not f.startswith("~")
                ]
                if pcb_candidates:
                    pcb_candidates.sort(key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
                    pcb_file = os.path.abspath(pcb_candidates[0])
            except OSError:
                pass

        if pcb_file:
            board_dir = os.path.dirname(pcb_file)
            base_name = os.path.splitext(os.path.basename(pcb_file))[0]
            matched_sch = os.path.join(board_dir, f"{base_name}.kicad_sch")
            if os.path.isfile(matched_sch):
                sch_file = matched_sch
            elif os.path.isdir(board_dir):
                try:
                    sch_candidates = [
                        os.path.join(board_dir, f)
                        for f in os.listdir(board_dir)
                        if f.endswith(".kicad_sch") and not f.startswith(".") and not f.startswith("~")
                    ]
                    if sch_candidates:
                        sch_candidates.sort(key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
                        sch_file = os.path.abspath(sch_candidates[0])
                except OSError:
                    pass

        return pcb_file, sch_file

    def _sync_file_availability_state(self):
        """
        Update UI control availability based on active project source files.

        Disables schematic-dependent outputs (Schematic PDF, Bill of Materials,
        and MPN fields) when exporting from a standalone board file without an
        accompanying schematic.
        """
        pcb_file, sch_file = self._resolve_project_source_files()
        has_sch = bool(sch_file and os.path.isfile(sch_file))
        sch_missing = bool(pcb_file and not has_sch)

        if hasattr(self, "chk_sch_pdf"):
            if sch_missing:
                self.chk_sch_pdf.SetValue(False)
                self.chk_sch_pdf.Disable()
                self.chk_sch_pdf.SetToolTip("Disabled: no matching .kicad_sch schematic file found")
            else:
                self.chk_sch_pdf.Enable()
                self.chk_sch_pdf.SetToolTip("")

        if hasattr(self, "chk_bom"):
            if sch_missing:
                self.chk_bom.SetValue(False)
                self.chk_bom.Disable()
                self.chk_bom.SetToolTip("Disabled: no matching .kicad_sch schematic file found")
            else:
                self.chk_bom.Enable()
                self.chk_bom.SetToolTip("")

        if hasattr(self, "chk_bom_mfr_mpn"):
            if sch_missing:
                self.chk_bom_mfr_mpn.Disable()
            else:
                self.chk_bom_mfr_mpn.Enable()

        self._sync_export_button_state()

    def _sync_export_button_state(self, has_outputs: bool | None = None):
        """
        Update the Export button state based on board file presence and selected outputs.

        Disables the button if no board file exists or if no output targets are selected.
        """
        if not hasattr(self, "btn_export"):
            return
        pcb_file, _ = self._resolve_project_source_files()
        has_pcb = bool(pcb_file and os.path.isfile(pcb_file))
        if has_outputs is None:
            has_outputs = any(
                getattr(self, self._export_checkbox_attr(key)).IsChecked()
                for key in _EXPORT_TOGGLE_KEYS
                if hasattr(self, self._export_checkbox_attr(key))
            )
        if getattr(self, "_export_running", False):
            self.btn_export.Disable()
            self.btn_export.SetToolTip("Export in progress...")
        elif not has_pcb:
            self.btn_export.Disable()
            self.btn_export.SetToolTip("Disabled: no .kicad_pcb board file found")
        elif not has_outputs:
            self.btn_export.Disable()
            self.btn_export.SetToolTip("Disabled: no export outputs selected")
        else:
            self.btn_export.Enable()
            self.btn_export.SetToolTip("")

    def on_gerbers_toggled(self, event):
        """Synchronize drill export state and refresh export summary on Gerber toggle."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self._sync_drill_checkbox_state()
        self.on_export_checkbox_changed(event)

    def on_homebrew_pdf_toggled(self, event):
        """Synchronize Copper SVG export state and refresh export summary on Homebrew PDF toggle."""
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
            if self.pcb_file and not os.path.abspath(self.pcb_file).startswith(os.path.abspath(project_dir)):
                self.pcb_file = None
            self._reload_settings(project_dir)
            self._settings_project_dir = project_dir
            self._refresh_cd_workflow_status()

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
            if self.pcb_file and not os.path.abspath(self.pcb_file).startswith(os.path.abspath(chosen_dir)):
                self.pcb_file = None
            self._reload_settings(chosen_dir)
            self._settings_project_dir = chosen_dir
            self._refresh_cd_workflow_status()
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
        self._show_status_message("Settings reset to built-in defaults.")

    def _export_options(self):
        """Build export and CD option flags from the current dialog state."""
        options = kiforge.apply_export_params_to_options(self._current_settings())
        if options['export_gerbers']:
            options['export_drills'] = True
        options['generate_cd'] = False
        return options

    def on_save_project_defaults(self, event):
        """Save current selections to the project .kiforge.json file."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            _message_box("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return
        try:
            curr = self._current_settings()
            kiforge.save_settings(curr, project_dir=project_dir, scope="project")
            self.settings = curr
            self._show_status_message("Project defaults saved.")
        except Exception as e:
            _message_box(f"Failed to save project settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR, parent=self)

    def on_save_global_defaults(self, event):
        """Save current selections to the user-wide KiForge settings file."""
        try:
            curr = self._current_settings()
            kiforge.save_settings(curr, scope="global")
            self.settings = curr
            self._show_status_message("Global defaults saved.")
        except Exception as e:
            _message_box(f"Failed to save global settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR, parent=self)

    def on_generate_cd(self, event):
        """Generate CD workflow YAML and update .gitignore from current selections."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            _message_box("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            _message_box("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        if self._has_existing_cd_workflows() and not getattr(event, "_skip_confirm", False):
            resp = _message_box(
                "Overwrite existing workflows with the current export selections?",
                "Update Workflows?",
                wx.YES_NO | wx.ICON_QUESTION,
                parent=self,
                btn_labels=("Cancel", "Update"),
            )
            if resp != wx.ID_YES:
                return

        cd_options = self._export_options()
        cd_options["generate_cd"] = True
        msg, success = kiforge.generate_cd_files(project_dir, output_dir_name, cd_options)
        if success:
            self._cd_unlocked = False
            self._refresh_cd_workflow_status()
            self._show_status_message("Release workflows updated.")
        else:
            _message_box(msg, "Error", wx.OK | wx.ICON_ERROR, parent=self)

    def on_run_export(self, event):
        """
        Runs the KiForge export pipeline in a background worker thread.
        Progress is polled with wx.Timer so KiCad's UI thread is not blocked.
        """
        if self._export_running:
            _message_box(
                "An export is already in progress.",
                "KiForge",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
            return

        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            _message_box("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            _message_box("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR, parent=self)
            return

        export_flags = self._export_options()

        state = {
            'running': True,
            'success': False,
            'error_msg': None,
            'val': 0,
            'msg': "Initializing...",
            'cancelled': False,
        }

        def progress_callback(step_index, total_steps, message):
            """Relay export progress updates to worker state dict."""
            if state.get("cancelled"):
                return False
            if step_index is not None and total_steps is not None and total_steps > 0:
                state["val"] = int((step_index / total_steps) * 100)
            if message:
                state["msg"] = message
            return not state.get("cancelled")

        context = kiforge.ExportContext(
            project_dir,
            output_dir_name,
            export_flags,
            progress_callback,
            pcb_file=self.pcb_file,
        )
        if not context.resolve():
            _message_box(
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
        self._export_close_after_finish = False
        self.btn_export.Disable()

        def request_cancel():
            """Abort the worker as soon as Cancel is pressed."""
            state["cancelled"] = True
            context.cancel()
            self._export_join_deadline = min(
                getattr(self, "_export_join_deadline", time.time() + 20), time.time() + 20
            )

        # The result dialog from the previous run stays up until dismissed, so
        # clear it before starting another export rather than leaking it.
        self._destroy_export_progress()
        progress = _ExportProgressDialog(self, on_cancel=request_cancel)
        self._export_progress = progress

        def export_worker():
            """Background thread executing the core KiForge export pipeline."""
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

        # Modal, and deliberately so. Studio itself runs under ShowModal(),
        # which on macOS is an application-modal Cocoa session: a modeless
        # child opened beneath it is drawn behind Studio and receives no mouse
        # events at all -- Cancel and OK did nothing, and the window kept
        # vanishing behind the one that spawned it. Hand-pumping events to keep
        # it alive only traded that for a flicker, because wx.SafeYield()
        # disables and re-enables every top-level window on each of the
        # thirteen ticks a second the poll timer runs at.
        #
        # A nested modal loop is what wx provides for exactly this: the dialog
        # is in front, it gets its own events, the poll timer below still
        # fires, and the export stays on its worker thread throughout. This
        # blocks until the dialog ends -- on Cancel once the worker unwinds, or
        # on OK once the result has been shown in it.
        try:
            progress.ShowModal()
        finally:
            self._stop_export_timer()
            if self._export_progress is progress:
                self._export_progress = None
            progress.Destroy()
            if hasattr(self, "btn_export"):
                self.btn_export.Enable()

    def _stop_export_timer(self):
        """Stop active progress poll timer if running."""
        if self._export_timer and self._export_timer.IsRunning():
            self._export_timer.Stop()

    def _destroy_export_progress(self):
        """Stop timer, dismiss progress dialog, and re-enable export button."""
        self._stop_export_timer()
        progress = self._export_progress
        self._export_progress = None
        _destroy_progress_dialog(progress)
        if hasattr(self, "btn_export"):
            self.btn_export.Enable()

    def _poll_export_progress(self, event):
        """Poll background export worker thread and update progress dialog state."""
        state = self._export_state
        context = self._export_context
        progress = self._export_progress
        thread = self._export_thread
        if not state or not context or not thread:
            self._destroy_export_progress()
            return

        # Backstop, not the primary path. Cancellation normally lands the
        # instant the button is pressed, through the on_cancel callback the
        # dialog is constructed with -- the poll timer cannot be relied on,
        # because a long export step starves it, which is exactly when Cancel
        # gets used. This branch still matters for a progress dialog built
        # without that callback, where it is the only thing that would ever
        # cancel; the `not state["cancelled"]` guard makes it a no-op once the
        # callback has already run.
        #
        # The dialog deliberately stays up after a cancel: the worker still has
        # to unwind the current step, and tearing the window down at that point
        # left Studio looking idle -- no progress window, Export still disabled
        # -- for as long as that took, which read as the cancel doing nothing.
        # _finish_export_progress() closes it when the worker actually exits.
        if progress and progress.was_cancelled() and not state["cancelled"]:
            state["cancelled"] = True
            context.cancel()
            self._export_join_deadline = min(self._export_join_deadline, time.time() + 20)

        if state["running"]:
            # Unconditional: the dialog decides whether this tick advances the
            # gauge or just pulses it (see _ExportProgressDialog.update).
            # Filtering unchanged ticks out here is what left the gauge frozen
            # for the whole duration of a long step.
            if progress:
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

        if progress and state.get("success") and not state.get("cancelled"):
            progress.update(100, state.get("msg") or "Completed successfully!")
        self._finish_export_progress()

    def _finish_export_progress(self):
        """Clean up worker thread state and schedule UI presentation of results."""
        if not self._export_running:
            return
        state = self._export_state
        context = self._export_context
        project_dir = self._export_project_dir
        if context and hasattr(context, "temp_gerber_dir") and context.temp_gerber_dir and os.path.isdir(context.temp_gerber_dir):
            try:
                shutil.rmtree(context.temp_gerber_dir)
            except Exception:
                pass
        self._export_running = False
        self._stop_export_timer()
        self._export_thread = None
        self._export_state = None
        self._export_context = None
        self._export_project_dir = None
        wx.CallAfter(self._finish_export_ui, state, context, project_dir)

    def _finish_export_ui(self, state, context, project_dir):
        """Present the export result in the progress dialog that ran the export."""
        if not self:
            return
        # Only close the whole Studio window when the user explicitly asked to
        # close it while an export was running (see on_close). A cancelled,
        # failed, or even successful export otherwise must leave Studio open
        # so the user can adjust settings and export again -- closing it here
        # unconditionally is what made Cancel look like it killed the plugin.
        #
        # The progress dialog's loop is nested inside Studio's, so it has to
        # end first: ending the outer loop while the inner one is still running
        # leaves wx unwinding them in the wrong order.
        if self._export_close_after_finish:
            self._destroy_export_progress()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            return

        if state and context:
            self._show_export_result(state, context, project_dir)
        else:
            self._destroy_export_progress()

    def _show_export_result(self, state, context, project_dir):
        """
        Report the outcome in the progress dialog, as one short line.

        One window for one operation: the dialog that showed the progress shows
        the result, its Cancel becomes OK, and there is no second popup
        appearing as the first disappears. Detail that does not fit one line --
        the full warning text, the failure traceback -- is already in the log;
        see kiforge.setup_logger().
        """
        progress = self._export_progress
        if progress is None:
            return

        if state['cancelled']:
            progress.show_result("Export cancelled.", complete=False)
            return

        if state['error_msg']:
            logger.error("Export failed: %s", state['error_msg'])
            progress.show_result(f"Export failed: {_first_line(state['error_msg'])}", complete=False)
            return

        if state['success']:
            # Shown as a path fragment ("/kiforge") rather than a bare name, so
            # it reads as a folder rather than a word in the sentence.
            # Separators are normalised first: os.path.basename only splits the
            # host platform's separator, and this string is for display, not
            # for opening anything.
            tail = context.output_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            folder = "/" + (tail or context.output_dir)
            if context.warnings:
                for warning in context.warnings:
                    logger.warning("Export warning: %s", warning)
                count = len(context.warnings)
                progress.show_result(
                    f"Export complete with {count} warning{'s' if count != 1 else ''}. "
                    f"Saved to {folder}."
                )
            else:
                progress.show_result(f"Export complete. Saved to {folder}.")
            return

        for warning in context.warnings:
            logger.warning("Export warning: %s", warning)
        summary = _first_line(context.warnings[0]) if context.warnings else "no steps completed"
        progress.show_result(f"Export failed: {summary}", complete=False)

    def on_close(self, event):
        """Triggered when the close button is clicked."""
        if self._export_running:
            if _message_box(
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
            self._export_close_after_finish = True
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
        # KiCad falls back to icon_file_name when dark_icon_file_name is unset,
        # but that fallback is not reliable across all KiCad releases/platforms.
        # macOS defaults to system dark mode far more often than Windows/Linux,
        # so an unset dark_icon_file_name is the most common way a plugin's
        # toolbar icon silently fails to render on a Mac. KiForge's icon already
        # has a fully opaque badge background, so the same file works for both
        # themes -- no separate dark artwork is needed.
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        """
        Executes the KiForge ActionPlugin when clicked inside the KiCad PCB Editor.
        Attempts to resolve the active project path from the pcbnew Board instance
        and presents the Settings GUI.
        """
        kiforge.setup_logger()
        logger.info("KiForge Studio action plugin invoked.")

        project_dir = None
        pcb_file = None
        try:
            board = pcbnew.GetBoard() if has_pcbnew else None
            if board:
                pcb_file = board.GetFileName()
                if pcb_file and pcb_file.endswith(".kicad_pcb"):
                    board_dir = os.path.dirname(pcb_file)
                    pro_file = pcb_file.replace(".kicad_pcb", ".kicad_pro")
                    if os.path.isfile(pro_file):
                        project_dir = os.path.dirname(pro_file)
                    else:
                        pro_files = [f for f in os.listdir(board_dir) if f.endswith(".kicad_pro")]
                        if pro_files:
                            project_dir = board_dir
                        else:
                            parent = os.path.dirname(board_dir)
                            if os.path.isdir(parent) and any(f.endswith(".kicad_pro") for f in os.listdir(parent)):
                                project_dir = parent
                            else:
                                project_dir = board_dir
        except Exception as e:
            logger.debug(f"Failed to resolve board filename from pcbnew context: {e}")

        if not project_dir:
            cwd = os.getcwd()
            pro_files = [f for f in os.listdir(cwd) if f.endswith(".kicad_pro")]
            if pro_files:
                project_dir = cwd

        parent_window = _kicad_parent_window()

        dialog = None
        try:
            import importlib
            mod_name = __name__
            if mod_name in sys.modules:
                mod = importlib.reload(sys.modules[mod_name])
                DlgClass = getattr(mod, "KiForgeStudioSettingsDialog", KiForgeStudioSettingsDialog)
            else:
                DlgClass = KiForgeStudioSettingsDialog
            dialog = DlgClass(parent_window, project_dir, pcb_file=pcb_file)
            dialog.ShowModal()
        except Exception as exc:
            logger.exception("KiForge Studio dialog failed to open.")
            try:
                _message_box(
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
    
    # Held in a local on purpose: the app must outlive ShowModal() below,
    # and dropping the reference would collect it immediately.
    app = wx.App(False)  # noqa: F841
    
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
