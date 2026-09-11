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
# Two mirrored Zinc ramps, chosen from the OS appearance at dialog open.
#
# Studio used to hardcode the dark ramp only. On a Mac running light mode that
# produced a dark dialog under a *light* system title bar, with every natively
# drawn element -- title bar, scrollbars, file dialogs, selection highlights --
# still light, and any text left at a system colour rendering dark-on-dark. The
# window looked broken and parts of it were unreadable. Windows users rarely saw
# it because the KiCad default there is dark.
#
# Both ramps must define exactly the same keys; a test enforces that, because a
# missing key would raise inside a paint handler where exceptions are swallowed.
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

# Every widget reads _COLORS inside a paint handler, never at import time, so
# the active palette is applied by mutating this dict in place -- all existing
# lookups pick the change up with no call-site edits and no stale references.
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

# Live severity colours, swapped in place by refresh_palette() exactly as
# _COLORS is. Defined here, beside the table it mirrors and above the function
# that mutates it, so the palette block is self-contained: a forward reference
# would only surface as a NameError at import, which KiCad's loader swallows.
_MSG_ICON_COLORS = dict(_MSG_ICON_COLORS_BY_MODE["dark"])

_palette_mode = "dark"


def _system_is_dark() -> bool:
    """
    True when the OS/KiCad appearance is dark.

    ``wx.SystemAppearance.IsDark`` is the supported query on wx 4.1+. The
    luminance fallback covers older builds and any backend that does not
    implement it, so this never raises inside a paint path.
    """
    try:
        return bool(wx.SystemSettings.GetAppearance().IsDark())
    except Exception:
        pass
    try:
        bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        luminance = 0.299 * bg.Red() + 0.587 * bg.Green() + 0.114 * bg.Blue()
        return luminance < 128
    except Exception:
        return True


def active_palette_mode() -> str:
    """Return the palette currently applied: ``"dark"`` or ``"light"``."""
    return _palette_mode


def refresh_palette() -> str:
    """
    Point :data:`_COLORS` at the ramp matching the current system appearance.

    Call before building widgets and again on ``wx.EVT_SYS_COLOUR_CHANGED``.
    Returns the mode applied. Safe to call before a ``wx.App`` exists -- it
    falls back to dark rather than raising.
    """
    global _palette_mode
    _palette_mode = "dark" if _system_is_dark() else "light"
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


class _FlatButton(wx.Panel):
    """Flat filled button with rounded corners; paints consistently on Windows and Linux."""

    def __init__(self, parent, label: str, *, primary: bool = False, min_width: int = 0):
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._primary = primary
        self._tone = None
        self._hover = False
        self._pressed = False
        self._enabled = True
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(parent.GetBackgroundColour() if parent else _COLORS["app_bg"])
        self.SetMinSize((min_width, _CTRL_H))
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
        return self._label

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
        """
        Capture can be taken away at any time (a modal dialog, a window switch).

        Only ``_pressed`` is a click-in-progress and must drop. Hover is a
        property of where the pointer is, so it is recomputed rather than
        cleared -- clearing it blanks the highlight on a control the pointer is
        still resting on, and EVT_ENTER_WINDOW has already fired so nothing
        restores it until the pointer leaves and comes back.
        """
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetSize()
        parent = self.GetParent()
        parent_bg = parent.GetBackgroundColour() if parent else _COLORS["app_bg"]
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()

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

        # Fill-only, like every other custom-painted control here: a 1px
        # stroke has to straddle the path boundary at a half-pixel offset,
        # which backends round inconsistently (see _FlatRadioButton._on_paint).
        # The border is a solid rounded rect with the fill inset on top.
        gc = wx.GraphicsContext.Create(dc)
        if gc:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(border))
            outer = gc.CreatePath()
            outer.AddRoundedRectangle(0, 0, width, height, _BUTTON_RADIUS)
            gc.DrawPath(outer)
            gc.SetBrush(wx.Brush(fill))
            inner = gc.CreatePath()
            inner.AddRoundedRectangle(1, 1, width - 2, height - 2, max(0, _BUTTON_RADIUS - 1))
            gc.DrawPath(inner)
        else:
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(border))
            dc.DrawRoundedRectangle(0, 0, width, height, _BUTTON_RADIUS)
            dc.SetBrush(wx.Brush(fill))
            dc.DrawRoundedRectangle(1, 1, width - 2, height - 2, max(0, _BUTTON_RADIUS - 1))

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


_CHECKBOX_GLYPH_SIZE = 16
_CHECKBOX_GLYPH_RADIUS = 4


def _checkmark_pen() -> "wx.Pen":
    """
    White stroke for the checkbox tick, with rounded ends.

    The default butt cap and mitre join leave a 16px tick with two blunt
    square ends and a notched outer corner where the two strokes meet, which
    is what made the glyph look chipped rather than drawn.
    """
    pen = wx.Pen(wx.Colour(255, 255, 255), 2)
    pen.SetCap(wx.CAP_ROUND)
    pen.SetJoin(wx.JOIN_ROUND)
    return pen


class _FlatCheckBox(wx.Panel):
    """
    Fully custom-painted checkbox: no native OS chrome behind the label, so
    there is no native focus rectangle to fight. wx.CheckBox's own dotted
    keyboard-focus rectangle on MSW does not respect WM_UPDATEUISTATE /
    UISF_HIDEFOCUS for this control/theme combination (confirmed by direct
    testing, not assumed), so suppressing it after the fact isn't reliable;
    owning the paint entirely -- the same approach _FlatButton already uses
    for buttons in this dialog -- sidesteps the problem instead of chasing it.

    Drop-in replacement for wx.CheckBox's IsChecked()/GetValue()/SetValue()/
    Enable()/Disable() and wx.EVT_CHECKBOX surface, so existing call sites
    (self.chk_x.IsChecked(), .SetValue(...), .Bind(wx.EVT_CHECKBOX, ...))
    need no changes beyond the constructor.
    """

    def __init__(self, parent, label: str = ""):
        super().__init__(parent, style=wx.BORDER_NONE)
        self._label = label
        self._checked = False
        self._hover = False
        self._pressed = False
        self._enabled = True
        self._has_focus = False
        self._focus_from_pointer = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetBackgroundColour(parent.GetBackgroundColour() if parent else _COLORS["app_bg"])

        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        text_w, text_h = dc.GetTextExtent(label) if label else (0, 0)
        gap = _SP_SM if label else 0
        self.SetMinSize((_CHECKBOX_GLYPH_SIZE + gap + text_w, max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SET_FOCUS, self._on_set_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def AcceptsFocus(self):
        return self._enabled

    def _on_set_focus(self, event):
        """
        Draw a focus ring for keyboard focus only.

        A ring after every mouse click is the "focus ring pollution" the
        platform itself avoids: click a control on macOS and no ring appears,
        Tab to it and one does. ``_focus_from_pointer`` is set by
        :meth:`_on_left_down` immediately before ``SetFocus()``, so a click
        still takes focus -- it just does not advertise it.
        """
        self._has_focus = not self._focus_from_pointer
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_kill_focus(self, event):
        """
        Take the state from the event, never from HasFocus().

        Inside EVT_KILL_FOCUS the focus transfer has not completed, so
        HasFocus() can still report True. Deriving the flag from it left the
        control that just lost focus permanently "focused", and since the paint
        code draws an accent ring for a focused glyph, the previously selected
        radio kept an orange ring after its dot had correctly cleared.
        """
        self._has_focus = False
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_key_down(self, event):
        if self._enabled and event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._toggle()
        else:
            event.Skip()

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
        self._focus_from_pointer = True
        self.SetFocus()
        self.Refresh()

    def _on_left_up(self, event):
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
            self._toggle()

    def _on_capture_lost(self, event):
        """
        Capture can be taken away at any time (a modal dialog, a window switch).

        Only ``_pressed`` is a click-in-progress and must drop. Hover is a
        property of where the pointer is, so it is recomputed rather than
        cleared -- clearing it blanks the highlight on a control the pointer is
        still resting on, and EVT_ENTER_WINDOW has already fired so nothing
        restores it until the pointer leaves and comes back.
        """
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _toggle(self):
        self._checked = not self._checked
        self.Refresh()
        event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, self.GetId())
        event.SetEventObject(self)
        event.SetInt(1 if self._checked else 0)
        wx.PostEvent(self, event)

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetSize()
        parent = self.GetParent()
        parent_bg = parent.GetBackgroundColour() if parent else _COLORS["app_bg"]
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()

        box_y = (height - _CHECKBOX_GLYPH_SIZE) // 2
        accent = _COLORS["accent"]

        focused = self._has_focus and self._enabled
        if not self._enabled:
            border = _COLORS["border"]
            fill = (
                wx.Colour(accent.Red() // 2, accent.Green() // 2, accent.Blue() // 2)
                if self._checked else _COLORS["input_bg"]
            )
            text_colour = _COLORS["muted"]
        elif self._checked:
            fill = accent
            # The glyph is already filled with the accent, so an accent border
            # is invisible -- keyboard focus needs a colour that contrasts with
            # the fill or Tab navigation has no visible position at all.
            border = _COLORS["text"] if focused else accent
            text_colour = _COLORS["text"]
        else:
            fill = _COLORS["input_bg"]
            # _COLORS["border"] (63,63,70) reads as barely-there against the
            # (24,24,27)/(39,39,42) panel background -- fine for a divider
            # line, too faint for an interactive control's idle outline.
            # _COLORS["muted"] is already the app's proven-visible dim
            # foreground (used for labels), reused here rather than adding
            # another one-off colour.
            border = accent if (self._hover or focused) else _COLORS["muted"]
            text_colour = _COLORS["text"]

        # Focus is shown as a bolder accent border directly on the glyph
        # itself (never a separate outline around the whole row -- that
        # reads as the same intrusive dotted-rectangle look this control
        # exists to avoid, and clips against the panel edge besides, since
        # the glyph sits flush against x=0 with no margin to draw outside of).
        border_width = 2 if focused else 1

        # Concentric filled shapes, never a stroked outline -- see the
        # matching comment in _FlatRadioButton._on_paint: a stroke centred on
        # a path boundary needs sub-pixel positioning at odd widths (a 1px
        # line straddling a boundary is two half-pixels), which graphics
        # backends are free to round asymmetrically, while a plain fill never
        # hits that ambiguity. Drawing the border as a solid rounded rect
        # with a smaller, inset rounded rect (fill colour) on top removes
        # that inconsistency instead of chasing one more coordinate.
        inner_inset = border_width
        inner_w = max(0, _CHECKBOX_GLYPH_SIZE - inner_inset * 2)
        inner_radius = max(0, _CHECKBOX_GLYPH_RADIUS - inner_inset)

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(border))
            outer_path = gc.CreatePath()
            outer_path.AddRoundedRectangle(0, box_y, _CHECKBOX_GLYPH_SIZE, _CHECKBOX_GLYPH_SIZE, _CHECKBOX_GLYPH_RADIUS)
            gc.DrawPath(outer_path)
            gc.SetBrush(wx.Brush(fill))
            inner_path = gc.CreatePath()
            inner_path.AddRoundedRectangle(inner_inset, box_y + inner_inset, inner_w, inner_w, inner_radius)
            gc.DrawPath(inner_path)
            if self._checked:
                # Proportional to the glyph size (not fixed pixels) so the
                # checkmark stays centred and correctly scaled if
                # _CHECKBOX_GLYPH_SIZE ever changes again.
                g = _CHECKBOX_GLYPH_SIZE
                gc.SetPen(_checkmark_pen())
                check = gc.CreatePath()
                check.MoveToPoint(0.22 * g, box_y + 0.5 * g)
                check.AddLineToPoint(0.42 * g, box_y + 0.72 * g)
                check.AddLineToPoint(0.78 * g, box_y + 0.28 * g)
                gc.StrokePath(check)
        else:
            # wx.GraphicsContext.Create() can legitimately return None -- most
            # commonly on a freshly-created window's very first paint, before
            # it has a realized native drawing surface -- and reliably
            # succeeds on every later repaint. This path must stay visually
            # complete on its own (checkmark, correct border weight) rather
            # than a stripped-down placeholder: a checked box that first
            # paints via this branch must still look checked, not empty until
            # the user happens to interact with it and trigger a GC repaint.
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(border))
            dc.DrawRoundedRectangle(0, box_y, _CHECKBOX_GLYPH_SIZE, _CHECKBOX_GLYPH_SIZE, _CHECKBOX_GLYPH_RADIUS)
            dc.SetBrush(wx.Brush(fill))
            dc.DrawRoundedRectangle(inner_inset, box_y + inner_inset, inner_w, inner_w, inner_radius)
            if self._checked:
                g = _CHECKBOX_GLYPH_SIZE
                dc.SetPen(_checkmark_pen())
                dc.DrawLine(int(0.22 * g), int(box_y + 0.5 * g), int(0.42 * g), int(box_y + 0.72 * g))
                dc.DrawLine(int(0.42 * g), int(box_y + 0.72 * g), int(0.78 * g), int(box_y + 0.28 * g))

        if self._label:
            dc.SetTextForeground(text_colour)
            dc.SetFont(self.GetFont())
            _tw, text_h = dc.GetTextExtent(self._label)
            dc.DrawText(self._label, _CHECKBOX_GLYPH_SIZE + _SP_SM, (height - text_h) // 2)

    # --- wx.CheckBox-compatible API ---
    def IsChecked(self) -> bool:
        return self._checked

    def GetValue(self) -> bool:
        return self._checked

    def SetValue(self, value: bool) -> None:
        self._checked = bool(value)
        self.Refresh()

    def Enable(self, enable=True):
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        return self.Enable(False)


class _FlatRadioButton(wx.Panel):
    """
    Fully custom-painted radio button -- same rationale as _FlatCheckBox: no
    native OS chrome means no native dotted focus rectangle to fight.

    Native wx.RadioButton groups siblings automatically via the wx.RB_GROUP
    style; since this control owns its own painting instead of wrapping a
    native radio control, group membership is explicit instead: pass the same
    ``group`` list to every button that should be mutually exclusive (each
    appends itself on construction), and selecting one clears the rest.
    """

    def __init__(self, parent, label: str = "", group: list | None = None):
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
        self.SetBackgroundColour(parent.GetBackgroundColour() if parent else _COLORS["app_bg"])

        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        text_w, text_h = dc.GetTextExtent(label) if label else (0, 0)
        gap = _SP_SM if label else 0
        self.SetMinSize((_CHECKBOX_GLYPH_SIZE + gap + text_w, max(_CHECKBOX_GLYPH_SIZE, text_h) + _SP_XS))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_ENTER_WINDOW, self._on_enter)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
        self.Bind(wx.EVT_SET_FOCUS, self._on_set_focus)
        self.Bind(wx.EVT_KILL_FOCUS, self._on_kill_focus)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def AcceptsFocus(self):
        return self._enabled

    def _on_set_focus(self, event):
        """
        Draw a focus ring for keyboard focus only.

        A ring after every mouse click is the "focus ring pollution" the
        platform itself avoids: click a control on macOS and no ring appears,
        Tab to it and one does. ``_focus_from_pointer`` is set by
        :meth:`_on_left_down` immediately before ``SetFocus()``, so a click
        still takes focus -- it just does not advertise it.
        """
        self._has_focus = not self._focus_from_pointer
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_kill_focus(self, event):
        """
        Take the state from the event, never from HasFocus().

        Inside EVT_KILL_FOCUS the focus transfer has not completed, so
        HasFocus() can still report True. Deriving the flag from it left the
        control that just lost focus permanently "focused", and since the paint
        code draws an accent ring for a focused glyph, the previously selected
        radio kept an orange ring after its dot had correctly cleared.
        """
        self._has_focus = False
        self._focus_from_pointer = False
        self.Refresh()
        event.Skip()

    def _on_key_down(self, event):
        if self._enabled and event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._select()
        else:
            event.Skip()

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
        self._focus_from_pointer = True
        self.SetFocus()
        self.Refresh()

    def _on_left_up(self, event):
        if not self._enabled:
            return
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self._pressed
        self._pressed = False
        # Recompute hover from where the pointer actually is. While the mouse
        # is captured the platform stops delivering EVT_LEAVE_WINDOW, so
        # _hover would otherwise stay True after the click and leave this
        # control's ring painted in the accent colour as though it were still
        # hovered -- it reads as a selection that will not clear.
        inside = self.ClientRect.Contains(event.GetPosition())
        self._hover = inside
        self.Refresh()
        if was_pressed and inside:
            self._select()

    def _on_capture_lost(self, event):
        """
        Capture can be taken away at any time (a modal dialog, a window switch).

        Only ``_pressed`` is a click-in-progress and must drop. Hover is a
        property of where the pointer is, so it is recomputed rather than
        cleared -- clearing it blanks the highlight on a control the pointer is
        still resting on, and EVT_ENTER_WINDOW has already fired so nothing
        restores it until the pointer leaves and comes back.
        """
        self._pressed = False
        self._hover = _pointer_is_inside(self)
        self.Refresh()

    def _apply_selection(self):
        """Select this button and clear its group siblings, without firing an event."""
        if self._selected:
            return
        for other in self._group:
            if other is not self and other._selected:
                other._selected = False
                # Recompute rather than trust the cached flag: if the pointer
                # left this control while another held the mouse capture, no
                # EVT_LEAVE_WINDOW was delivered and _hover is still True, so
                # the deselected radio keeps an accent ring until it is hovered
                # again.
                other._hover = _pointer_is_inside(other)
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
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetSize()
        parent = self.GetParent()
        parent_bg = parent.GetBackgroundColour() if parent else _COLORS["app_bg"]
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()

        box_y = (height - _CHECKBOX_GLYPH_SIZE) // 2
        accent = _COLORS["accent"]
        cx, cy, r = _CHECKBOX_GLYPH_SIZE / 2, box_y + _CHECKBOX_GLYPH_SIZE / 2, _CHECKBOX_GLYPH_SIZE / 2 - 1

        focused = self._has_focus and self._enabled
        if not self._enabled:
            border = _COLORS["border"]
            dot = _COLORS["muted"]
            text_colour = _COLORS["muted"]
        elif self._selected:
            border = accent
            dot = accent
            text_colour = _COLORS["text"]
        else:
            border = accent if (self._hover or focused) else _COLORS["muted"]
            dot = None
            text_colour = _COLORS["text"]

        # Focus is a bolder accent ring directly on the glyph -- see the
        # matching comment in _FlatCheckBox._on_paint for why this replaced
        # a separate dashed outline around the whole row.
        border_width = 2 if focused else 1

        # Concentric filled discs, never a stroked outline: a stroke and a
        # fill are two different rasterization paths, and graphics backends
        # are free to apply pixel-snapping/hinting to a stroked edge that a
        # plain fill never gets -- on at least one real combination of
        # Windows display scaling + wx build, that alone was enough to
        # visibly displace the ring from the dot even though both were fed
        # numerically identical centre coordinates. Painting the ring as a
        # solid disc (ring colour) with a smaller disc (background colour)
        # punched out on top -- then the dot on top of that, all three via
        # the exact same fill-only call -- removes that entire class of
        # stroke-vs-fill inconsistency instead of chasing one more
        # coordinate rounding case.
        dr = round(r * 0.5)
        inner_r = r - border_width

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(border))
            gc.DrawEllipse(cx - r, cy - r, r * 2, r * 2)
            gc.SetBrush(wx.Brush(_COLORS["input_bg"]))
            gc.DrawEllipse(cx - inner_r, cy - inner_r, inner_r * 2, inner_r * 2)
            if dot is not None:
                gc.SetBrush(wx.Brush(dot))
                gc.DrawEllipse(cx - dr, cy - dr, dr * 2, dr * 2)
        else:
            # Same GC-unavailable fallback as _FlatCheckBox -- must stay
            # visually complete (including focus border weight) on its own.
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(border))
            dc.DrawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
            dc.SetBrush(wx.Brush(_COLORS["input_bg"]))
            dc.DrawEllipse(int(cx - inner_r), int(cy - inner_r), int(inner_r * 2), int(inner_r * 2))
            if dot is not None:
                dc.SetBrush(wx.Brush(dot))
                dc.DrawEllipse(int(cx - dr), int(cy - dr), int(dr * 2), int(dr * 2))

        if self._label:
            dc.SetTextForeground(text_colour)
            dc.SetFont(self.GetFont())
            _tw, text_h = dc.GetTextExtent(self._label)
            dc.DrawText(self._label, _CHECKBOX_GLYPH_SIZE + _SP_SM, (height - text_h) // 2)

    # --- wx.RadioButton-compatible API ---
    def GetValue(self) -> bool:
        return self._selected

    def SetValue(self, value: bool) -> None:
        """Programmatic selection -- matches wx.RadioButton.SetValue(): no event fired."""
        if value:
            self._apply_selection()
        elif self._selected:
            self._selected = False
            self.Refresh()

    def Enable(self, enable=True):
        self._enabled = bool(enable)
        self.Refresh()
        return super().Enable(enable)

    def Disable(self):
        return self.Enable(False)


class _ExportProgressDialog(wx.Dialog):
    """Non-modal export progress window following the system appearance."""

    def __init__(self, parent, on_cancel=None):
        super().__init__(
            parent,
            title="KiForge",
            style=wx.DEFAULT_DIALOG_STYLE,
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

        # Message, gauge and the action row all carry the same _SP_LG side
        # margin, so their left and right edges line up. The button used to
        # supply its own smaller wx.ALL border instead of the row taking the
        # container margin, which left it sitting 8px further right than the
        # gauge above it.
        self.lbl_message = wx.StaticText(self, label="Initializing exporter…")
        self.lbl_message.SetForegroundColour(_COLORS["text"])
        sizer.Add(self.lbl_message, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _SP_LG)

        self._gauge_spacer = sizer.AddSpacer(_SP_MD)
        self.gauge = wx.Gauge(self, range=100, size=(-1, _SP_SM))
        sizer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _SP_LG)

        # _SP_XL before the actions, matching _KiForgeMessageDialog.
        sizer.AddSpacer(_SP_XL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.AddStretchSpacer()
        self.btn_cancel = _FlatButton(self, "Cancel", min_width=88)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        row.Add(self.btn_cancel, 0)
        # Escape and the titlebar close arrive here. While the export runs they
        # mean "cancel", not "close": closing the window would leave the worker
        # running with nothing left to report it. Once the result is up they
        # mean the same thing as OK.
        self.Bind(wx.EVT_CLOSE, self._on_close_request)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _SP_LG)

        self.SetSizer(sizer)
        self.SetMinSize((340, -1))
        self.Fit()
        self.CentreOnParent()

    def _on_cancel(self, event):
        if self._cancelled:
            return
        self._cancelled = True
        self._message = "Cancelling…"
        self.lbl_message.SetLabel(self._message)
        self.gauge.Hide()
        if hasattr(self, "_gauge_spacer") and self._gauge_spacer is not None:
            self._gauge_spacer.Show(False)
        self.btn_cancel.Disable()
        self.Layout()
        self.Fit()
        # Paint "Cancelling..." now rather than whenever the loop next idles,
        # and abort the export straight away instead of waiting for the poll
        # timer to notice was_cancelled().
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
        self.lbl_message.SetLabel(message)
        if not complete or self._cancelled:
            self.gauge.Hide()
            if hasattr(self, "_gauge_spacer") and self._gauge_spacer is not None:
                self._gauge_spacer.Show(False)
        else:
            self.gauge.SetValue(100)
        self.btn_cancel.SetLabel("OK")
        self.btn_cancel.Enable()
        # Green confirms the run finished; a failure or cancellation keeps the
        # neutral button so the colour means something.
        self.btn_cancel.SetTone(_COLORS["success"] if complete else None)
        # Unbind by handler, not by event type: the bare form does not reliably
        # remove the binding, which would leave _on_cancel firing first and
        # turning the click into "Cancelling..." instead of dismissing.
        self.btn_cancel.Unbind(wx.EVT_BUTTON, handler=self._on_cancel)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_dismiss)
        self.Layout()
        self.Fit()
        self.Update()

    def is_finished(self) -> bool:
        return self._finished

    def was_cancelled(self) -> bool:
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
_MSG_TEXT_WRAP = 300
_MSG_MIN_WIDTH = 280
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

    def __init__(self, parent, message: str, title: str, kind: str, buttons: str):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        # Can be raised without the settings dialog ever opening (an export
        # failure from the toolbar), so it resolves the palette itself.
        refresh_palette()
        self.SetBackgroundColour(_COLORS["app_bg"])

        outer = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)

        msg = wx.StaticText(self, label=message)
        msg.SetForegroundColour(_COLORS["text"])
        msg.Wrap(_MSG_TEXT_WRAP)

        # A one-line message reads best with the glyph centred against it; once
        # the text wraps, centring against the whole block leaves the glyph
        # floating beside the middle of a paragraph, so anchor it to the top
        # (i.e. beside the first line) instead. Decided from the measured text
        # height rather than a guess about how long callers' messages are.
        multiline = msg.GetBestSize().height > msg.GetCharHeight() * 1.5
        icon_align = wx.ALIGN_TOP if multiline else wx.ALIGN_CENTER_VERTICAL

        icon_bmp = _load_message_icon_bitmap(kind)
        if icon_bmp is not None:
            row.Add(wx.StaticBitmap(self, bitmap=icon_bmp), 0, icon_align | wx.RIGHT, _SP_LG)
        row.Add(msg, 1, icon_align)

        # Proportion 0: the content row keeps its own natural height instead of
        # stretching to fill whatever extra space Fit()/SetMinSize would leave,
        # which is what left the icon stranded at the top with a dead gap below
        # it and the button row floating disconnected at the bottom.
        outer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _SP_LG)

        # _SP_XL here against _SP_LG everywhere else: the action row should read
        # as a separate area, not as one more line of the message body. A
        # sizer border can only carry a single width across the sides it names,
        # so the wider gap is its own spacer rather than a border on either row.
        outer.AddSpacer(_SP_XL)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.AddStretchSpacer()
        default_btn = self._add_buttons(btn_row, buttons)
        outer.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, _SP_LG)

        self.SetSizer(outer)
        # Width floor only -- height comes from Fit(). Kept just wide enough to
        # keep the title bar and action row from cramping; anything larger only
        # padded short messages ("Export cancelled.") out with dead space.
        self.SetMinSize((_MSG_MIN_WIDTH, -1))
        self.Fit()
        if parent:
            self.CentreOnParent()
        else:
            self.Centre()
        wx.CallAfter(default_btn.SetFocus)

    def _add_buttons(self, btn_row: wx.BoxSizer, buttons: str) -> "_FlatButton":
        if buttons == "yes_no":
            btn_no = _FlatButton(self, "No", min_width=80)
            btn_no.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_NO))
            btn_row.Add(btn_no, 0, wx.RIGHT, _SP_SM)
            btn_yes = _FlatButton(self, "Yes", primary=True, min_width=80)
            btn_yes.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_YES))
            btn_row.Add(btn_yes, 0)
            self.Bind(wx.EVT_CLOSE, lambda e: self.EndModal(wx.ID_NO))
            return btn_yes
        btn_ok = _FlatButton(self, "OK", primary=True, min_width=80)
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
    dlg = _KiForgeMessageDialog(parent, message, caption, kind, buttons)
    try:
        return dlg.ShowModal()
    finally:
        dlg.Destroy()


_DIALOG_MIN_WIDTH = 420
_DIALOG_MIN_HEIGHT = 380

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


def _load_tab_icon_bitmap(name: str, size: int = 20) -> wx.Bitmap | None:
    """Rasterize a bundled Material Symbol for notebook tabs, tinted for the theme."""
    # The tint is part of the key: a near-white glyph cached under dark mode is
    # invisible once the palette flips to light.
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
        Initializes the settings dialog window.
        
        Args:
            parent: The parent wxWindow or None if running standalone.
            project_dir (str, optional): Pre-resolved project root folder.
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
        self.pcb_file = pcb_file
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

    def _check_dependencies_async(self):
        """Check and install missing PDF renderer dependencies (Pillow) in the background."""
        def worker():
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
        self.SetBackgroundColour(_COLORS["app_bg"])
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        main_sizer.Add(self._build_header_panel(), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, _SP_LG)
        main_sizer.Add(self._separator(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, _SP_LG)

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
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, _SP_LG)
        main_sizer.Add(self._separator(self), 0, wx.EXPAND)
        main_sizer.Add(self._build_footer_panel(), 0, wx.EXPAND)

        self.SetSizer(main_sizer)
        self.SetMinSize((_DIALOG_MIN_WIDTH, _DIALOG_MIN_HEIGHT))

        self.Bind(wx.EVT_SIZE, self._on_dialog_resize)
        self.Bind(wx.EVT_SIZING, self._on_dialog_sizing)
        self._cd_sync_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_cd_sync_timer, self._cd_sync_timer)
        self._bind_live_cd_sync_handlers()
        self._attach_tab_icons()

    def _on_system_colour_changed(self, event):
        """Re-resolve the palette and repaint when the OS theme flips live."""
        refresh_palette()
        self.SetBackgroundColour(_COLORS["app_bg"])
        self._apply_notebook_icons()
        self.Refresh()
        event.Skip()

    def _attach_tab_icons(self):
        """
        Put icons on the notebook tabs.

        Icons ship inside the plugin, so the normal path is a local file read
        and runs inline. Only an icon absent from the package -- one added after
        this release -- falls back to a background CDN warm-up, which keeps the
        network off the dialog-open path entirely.
        """
        missing = [n for n in _TAB_ICON_NAMES if not kiforge.read_bundled_tab_icon_svg(n)]
        if not missing:
            self._apply_notebook_icons()
            return

        def worker():
            for name in missing:
                if kiforge.read_cached_tab_icon_svg(name):
                    continue
                kiforge.download_tab_icon_svg(name)
            wx.CallAfter(self._apply_notebook_icons)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_notebook_icons(self):
        """Attach the bundled Material Symbols to notebook tabs, tinted for the theme."""
        display_size = 20
        # No cache eviction: the tint is part of the key, so a theme flip is
        # already a miss and re-rasterizes. Evicting the current tint's entry
        # would only throw away the bitmaps this call is about to use.
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
            target.SetFocusIgnoringChildren()
            event.Skip()

        event_source.Bind(wx.EVT_LEFT_DOWN, _on_click)

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

    def _section_label(self, parent, text: str) -> wx.StaticText:
        lbl = wx.StaticText(parent, label=text)
        lbl.SetForegroundColour(_COLORS["muted"])
        font = lbl.GetFont()
        font.SetWeight(wx.FONTWEIGHT_NORMAL)
        lbl.SetFont(font)
        self._clear_focus_on_background_click(lbl, parent)
        return lbl

    def _muted_label(self, parent, text: str, wrap: int | None = None) -> wx.StaticText:
        lbl = wx.StaticText(parent, label=text)
        self._style_text(lbl, muted=True)
        if wrap:
            lbl.Wrap(wrap)
        self._clear_focus_on_background_click(lbl, parent)
        return lbl

    def _build_header_panel(self):
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
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        scroll = wx.ScrolledWindow(page, style=wx.VSCROLL)
        scroll.SetScrollRate(0, 10)
        self._style_panel(scroll, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT

        sizer.Add(self._section_label(scroll, "Project"), 0, inset | wx.TOP, _SP_SM)
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
        sizer.AddSpacer(_SP_SM)

        sizer.Add(self._section_label(scroll, "Output folder"), 0, inset, 0)
        self.txt_output_dir = wx.TextCtrl(scroll)
        self._style_input(self.txt_output_dir)
        self.txt_output_dir.SetValue(self.settings.get("output_dir", "kiforge"))
        sizer.Add(self.txt_output_dir, 0, wx.EXPAND | inset | wx.TOP, _SP_SM)
        sizer.AddSpacer(_SP_LG)

        sizer.Add(self._section_label(scroll, "Preset"), 0, inset, 0)
        self._preset_radios = []
        for label in EXPORT_PRESET_RADIO_LABELS:
            # Appends itself to self._preset_radios and joins that group for
            # mutual exclusivity -- see _FlatRadioButton's group parameter.
            rb = _FlatRadioButton(scroll, label=label, group=self._preset_radios)
            rb.Bind(wx.EVT_RADIOBUTTON, self.on_preset_changed)
            sizer.Add(rb, 0, inset | wx.TOP, _SP_XS)

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
        sizer.Add(self.lbl_export_summary, 0, wx.EXPAND | inset | wx.TOP, _SP_SM)

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
        self.choice_pos_side = wx.Choice(scroll, choices=["Both", "Front", "Back"])
        self.choice_pos_side.SetBackgroundColour(_COLORS["input_bg"])
        self.choice_pos_side.SetForegroundColour(_COLORS["input_fg"])
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
        self.notebook.AddPage(page, "Advanced")

    def _build_releases_tab(self):
        page = wx.Panel(self.notebook)
        self._style_panel(page, surface=False)
        sizer = wx.BoxSizer(wx.VERTICAL)
        inset = wx.LEFT | wx.RIGHT | wx.TOP

        sizer.Add(self._section_label(page, "Releases"), 0, inset, _SP_SM)
        btn_generate_cd = _FlatButton(page, "Set up workflows", primary=True, min_width=160)
        btn_generate_cd.Bind(wx.EVT_BUTTON, self.on_generate_cd)
        sizer.Add(btn_generate_cd, 0, inset | wx.TOP, _SP_SM)

        self.chk_generate_cd = _FlatCheckBox(page, label="Sync with export settings")
        sizer.Add(self.chk_generate_cd, 0, inset | wx.TOP, _SP_SM)

        self.lbl_cd_sync_status = wx.StaticText(page, label="")
        self._style_text(self.lbl_cd_sync_status, muted=True)
        self._clear_focus_on_background_click(self.lbl_cd_sync_status, page)
        sizer.Add(self.lbl_cd_sync_status, 0, inset | wx.TOP, _SP_SM)

        page.SetSizer(sizer)
        self.notebook.AddPage(page, "Releases")

    def _build_footer_panel(self):
        footer = wx.Panel(self)
        footer.SetBackgroundColour(_COLORS["footer_bg"])
        self._clear_focus_on_background_click(footer)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_save = _FlatButton(footer, "Save", min_width=64)
        btn_save.Bind(wx.EVT_BUTTON, self.on_settings_menu)

        self.btn_export = _FlatButton(footer, "Export", primary=True, min_width=72)
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_run_export)

        btn_close = _FlatButton(footer, "Close", min_width=64)
        btn_close.Bind(wx.EVT_BUTTON, self.on_close)

        sizer.Add(btn_save, 0, wx.ALL, _SP_SM)
        sizer.AddStretchSpacer()
        sizer.Add(self.btn_export, 0, wx.ALL, _SP_SM)
        sizer.Add(btn_close, 0, wx.ALL, _SP_SM)
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
        if self._initializing:
            return
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
            "export_homebrew_pdf": "chk_homebrew_pdf",
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
        return content + chrome

    def _fit_dialog_to_screen(self):
        try:
            display_w, display_h = wx.DisplaySize()
        except Exception:
            display_w, display_h = 1024, 768
        # Lay out first so the sizers can report real content sizes.
        self.Layout()
        # Snapped: the display can be any size, so deriving from it can land
        # off-grid even though the constants either side of it do not.
        width = _snap_to_grid(min(540, max(_DIALOG_MIN_WIDTH, display_w - 80)))
        height = _snap_to_grid(
            min(max(_DIALOG_MIN_HEIGHT, display_h - 100),
                max(_DIALOG_MIN_HEIGHT, self._natural_height()))
        )
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
        if self._initializing:
            return
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
        self.chk_generate_cd.SetValue(
            self._export_setting('generate_cd', self.settings.get('generate_ci', True))
        )
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
        """Homebrew PDF is generated from the Copper SVG layers, so Copper SVG
        export is required whenever Homebrew PDF is enabled -- lock it on
        (checked, disabled) rather than let the two drift out of sync."""
        if self.chk_homebrew_pdf.IsChecked():
            self.chk_svg.SetValue(True)
            self.chk_svg.Disable()
        else:
            self.chk_svg.Enable()

    def _resolve_active_files(self) -> tuple[str | None, str | None]:
        """Resolve active (.kicad_pcb, .kicad_sch) file paths for current project or pcb_file."""
        board_file = None
        sch_file = None
        if self.pcb_file and os.path.isfile(self.pcb_file):
            board_file = os.path.abspath(self.pcb_file)
        elif self.project_dir and os.path.isfile(self.project_dir) and self.project_dir.endswith(".kicad_pcb"):
            board_file = os.path.abspath(self.project_dir)
        elif self.project_dir and os.path.isdir(self.project_dir):
            try:
                pcb_files = [
                    os.path.join(self.project_dir, f)
                    for f in os.listdir(self.project_dir)
                    if f.endswith(".kicad_pcb") and not f.startswith(".") and not f.startswith("~")
                ]
                if pcb_files:
                    pcb_files.sort(key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
                    board_file = os.path.abspath(pcb_files[0])
            except OSError:
                pass

        if board_file:
            board_dir = os.path.dirname(board_file)
            base_name = os.path.splitext(os.path.basename(board_file))[0]
            candidate = os.path.join(board_dir, f"{base_name}.kicad_sch")
            if os.path.isfile(candidate):
                sch_file = candidate
            elif os.path.isdir(board_dir):
                try:
                    sch_files = [
                        os.path.join(board_dir, f)
                        for f in os.listdir(board_dir)
                        if f.endswith(".kicad_sch") and not f.startswith(".") and not f.startswith("~")
                    ]
                    if sch_files:
                        sch_files.sort(key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
                        sch_file = os.path.abspath(sch_files[0])
                except OSError:
                    pass

        if not sch_file and self.project_dir and os.path.isdir(self.project_dir):
            try:
                sch_files = [
                    os.path.join(self.project_dir, f)
                    for f in os.listdir(self.project_dir)
                    if f.endswith(".kicad_sch") and not f.startswith(".") and not f.startswith("~")
                ]
                if sch_files:
                    sch_files.sort(key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
                    sch_file = os.path.abspath(sch_files[0])
            except OSError:
                pass

        return board_file, sch_file

    def _sync_file_availability_state(self):
        """
        Dynamically enable/disable UI controls based on available files.
        When schematic is absent, disable schematic-dependent controls (Schematic PDF, BOM).
        """
        board_file, sch_file = self._resolve_active_files()
        has_sch = bool(sch_file and os.path.isfile(sch_file))
        sch_missing = bool(board_file and not has_sch)

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
        """Enable or disable the Export button based on board file and output selections."""
        if not hasattr(self, "btn_export"):
            return
        board_file, _ = self._resolve_active_files()
        has_pcb = bool(board_file and os.path.isfile(board_file))
        if has_outputs is None:
            has_outputs = any(
                getattr(self, self._export_checkbox_attr(key)).IsChecked()
                for key in _EXPORT_TOGGLE_KEYS
                if hasattr(self, self._export_checkbox_attr(key))
            )
        if not has_pcb:
            self.btn_export.Disable()
            self.btn_export.SetToolTip("Disabled: no .kicad_pcb board file found")
        elif not has_outputs:
            self.btn_export.Disable()
            self.btn_export.SetToolTip("Disabled: no export outputs selected")
        else:
            self.btn_export.Enable()
            self.btn_export.SetToolTip("")

    def on_gerbers_toggled(self, event):
        """Keep drill export aligned with Gerber export requirements."""
        if event is not None and hasattr(event, "Skip"):
            event.Skip()
        self._sync_drill_checkbox_state()
        self.on_export_checkbox_changed(event)

    def on_homebrew_pdf_toggled(self, event):
        """Keep Copper SVG export aligned with Homebrew PDF requirements."""
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
        _message_box("Dialog reset to built-in defaults.", "Reset", wx.OK | wx.ICON_INFORMATION)

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
            _message_box("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return
        try:
            curr = self._current_settings()
            kiforge.save_settings(curr, project_dir=project_dir, scope="project")
            self.settings = curr
            _message_box("Project defaults saved.", "Config Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            _message_box(f"Failed to save project settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_save_global_defaults(self, event):
        """Save current selections to the user-wide KiForge settings file."""
        try:
            curr = self._current_settings()
            kiforge.save_settings(curr, scope="global")
            self.settings = curr
            _message_box("Global defaults saved.", "Config Saved", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            _message_box(f"Failed to save global settings:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_generate_cd(self, event):
        """Generate CD workflow YAML and update .gitignore from current selections."""
        project_dir = self.txt_project_dir.GetValue().strip()
        if not project_dir or not os.path.isdir(project_dir):
            _message_box("Please select a valid KiCad project directory first.", "Error", wx.OK | wx.ICON_ERROR)
            return

        output_dir_name = self.txt_output_dir.GetValue().strip()
        if not output_dir_name:
            _message_box("Please specify a valid output directory name.", "Error", wx.OK | wx.ICON_ERROR)
            return

        msg, success = kiforge.generate_cd_files(project_dir, output_dir_name, self._export_options())
        if success:
            _message_box(msg, "CD Files Generated", wx.OK | wx.ICON_INFORMATION)
        else:
            _message_box(msg, "Error", wx.OK | wx.ICON_ERROR)

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
        if self._export_timer and self._export_timer.IsRunning():
            self._export_timer.Stop()

    def _destroy_export_progress(self):
        self._stop_export_timer()
        progress = self._export_progress
        self._export_progress = None
        _destroy_progress_dialog(progress)
        if hasattr(self, "btn_export"):
            self.btn_export.Enable()

    def _poll_export_progress(self, event):
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

        self._finish_export_progress()

    def _finish_export_progress(self):
        if not self._export_running:
            return
        state = self._export_state
        context = self._export_context
        project_dir = self._export_project_dir
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
        board_file = None
        try:
            board = pcbnew.GetBoard() if has_pcbnew else None
            if board:
                board_file = board.GetFileName()
                if board_file and board_file.endswith(".kicad_pcb"):
                    board_dir = os.path.dirname(board_file)
                    pro_file = board_file.replace(".kicad_pcb", ".kicad_pro")
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
            dialog = KiForgeStudioSettingsDialog(parent_window, project_dir, pcb_file=board_file)
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
