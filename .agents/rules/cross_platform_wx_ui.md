# Cross-Platform wxPython UI Invariants

## Invariant
Studio UI is a single shared codebase across Windows (`wxMSW`), Linux (`wxGTK`), and macOS (`wxMac`). Never create platform silos (e.g. separate Windows UI vs Linux UI files). All OS toolkit quirks must be encapsulated in shared helper functions within the UI.

## Core Rules

### 1. Widget Realization Timing (GTK3 Guard)
- **Never allocate `wx.ClientDC(self)` or query text metrics during widget `__init__`**.
- On Linux wxGTK, the underlying `GdkWindow` is lazy and is not yet realized during object construction. Calling `ClientDC` or `GetTextExtent` before realization triggers GTK critical assertions (`gdk_window_get_origin: assertion 'GDK_IS_WINDOW (window)' failed`) and returns zero font extents.
- Always use `_hal_measure_text(window, text, font)`: safely measures text and falls back to `wx.ScreenDC()` if the window is unmapped or unrealized.

### 2. Sub-Pixel Stroke Elimination (Cairo vs GDI)
- **Never draw 1px borders with stroked pens on integer coordinates**.
- Cairo (Linux) centers lines on half-pixels (`0.5, 0.5`), which causes 1px stroked borders to blur across 2 pixels or get clipped on right/bottom edges. Windows GDI snaps integer coordinates to pixel boundaries.
- Use **concentric solid fills** (`_hal_draw_rounded_rect` and `_hal_draw_circle`): draw the outer rounded rectangle in the border color, then draw the inner rounded rectangle 1px smaller in the background fill. Both Cairo and GDI render solid fills cleanly with 1:1 pixel accuracy.

### 3. Container Background Resolution (GTK Theme Guard)
- On Linux GTK3, nested panels and `wx.ScrolledWindow` often use transparent CSS stylesheets. Calling `self.GetBackgroundColour()` directly returns `wx.NullColour` (uninitialized), which causes dark or garbled paint patches.
- Always resolve background colors via `_hal_resolve_bg(window)`, which walks up the parent hierarchy until a concrete, valid `wx.Colour` is found.

### 4. Dynamic Sizer Sizing (Pango vs DirectWrite Font Metrics)
- Default Linux system fonts (Ubuntu, Cantarell via Fontconfig/Pango) have much larger line heights and descenders than Windows Segoe UI.
- Never hardcode fixed widget pixel heights (e.g. `SetMinSize((100, 28))`). Sizers and dynamic font measurement must dictate geometry so labels and controls are never clipped.
