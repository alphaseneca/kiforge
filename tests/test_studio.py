"""
GUI tests for KiForge Studio (wx dialog and CD sync).

Opt in with ``KIFORGE_RUN_GUI_TESTS=1`` — requires a display and wxPython.
"""
import unittest
import sys
import os
import tempfile
import json
import shutil
import threading
import time
from unittest.mock import MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import kiforge

# GUI tests need a working wx display; opt in with KIFORGE_RUN_GUI_TESTS=1
if os.environ.get("KIFORGE_RUN_GUI_TESTS") != "1":
    raise unittest.SkipTest("GUI tests skipped; set KIFORGE_RUN_GUI_TESTS=1 to run")

# pyrefly: ignore [missing-import]
import wx

from plugins import kiforge_studio

class TestKiForgeStudio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # package_plugin.py copies the root kiforge.py to plugins/kiforge.py for
        # local plugin development, and kiforge_studio's `from . import kiforge`
        # prefers that sibling over the root module these tests import and patch.
        # A stale copy therefore makes Studio tests exercise old code and fail in
        # ways that look like product regressions -- it cost real debugging time
        # once already. Fail loudly with the remedy instead.
        sibling = os.path.join(os.path.dirname(__file__), "..", "plugins", "kiforge.py")
        if os.path.isfile(sibling):
            bound = getattr(kiforge_studio.kiforge, "__file__", "")
            if os.path.basename(os.path.dirname(os.path.abspath(bound))) == "plugins":
                raise unittest.SkipTest(
                    "plugins/kiforge.py (a build artifact from package_plugin.py) is "
                    "shadowing the root kiforge module, so these tests would run "
                    "against a stale copy. Delete it and re-run: rm plugins/kiforge.py"
                )

        # Create a wx app that stays alive for all tests so wx widgets can be instantiated
        cls.app = wx.App(False)
        # Mock the themed message dialog to prevent modal popups blocking automated tests
        cls.original_message_box = kiforge_studio._message_box
        kiforge_studio._message_box = lambda *args, **kwargs: wx.OK

    @classmethod
    def tearDownClass(cls):
        # Restore the original themed message dialog
        kiforge_studio._message_box = cls.original_message_box

    def setUp(self):
        # Create a temporary directory representing a KiCad project
        self.test_dir = tempfile.mkdtemp()

        # Isolate every test from the real machine's global settings file.
        # Several tests exercise save/load of "global" scope; without this,
        # they read and write the actual developer/CI machine's
        # %APPDATA%/kiforge/settings.json (~/.config/kiforge on Linux/macOS),
        # which both pollutes real user state and makes unrelated tests
        # (e.g. ones asserting default values) depend on whatever that file
        # happens to contain from a previous run or from actually using
        # KiForge on this machine.
        self._global_settings_dir = tempfile.mkdtemp()
        fake_global_path = os.path.join(self._global_settings_dir, "settings.json")
        self._global_settings_patcher = patch.object(
            kiforge, "get_global_settings_path", return_value=fake_global_path
        )
        self._global_settings_patcher.start()

    def tearDown(self):
        self._global_settings_patcher.stop()
        shutil.rmtree(self._global_settings_dir, ignore_errors=True)
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    def test_load_default_settings_with_no_project(self):
        """Verify default settings are returned if project directory is empty/None."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, None)
        settings = dialog.settings
        self.assertEqual(settings['output_dir'], 'kiforge')
        self.assertTrue(settings['export_gerbers'])
        self.assertTrue(settings['format_jlc'])
        self.assertTrue(settings['generate_cd'])
        dialog.Destroy()

    def test_save_and_load_settings(self):
        """Verify settings are saved and loaded correctly to/from .kiforge.json."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        
        # Modify settings and save manually
        settings = {
            'output_dir': 'custom_out',
            'export_gerbers': False,
            'export_drills': True,
            'export_pos': False,
            'export_bom': True,
            'export_ibom': False,
            'export_sch_pdf': True,
            'export_step': False,
            'export_3d': True,
            'export_svg': False,
        }
        
        settings_file = os.path.join(self.test_dir, ".kiforge.json")
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(settings, f)
            
        # Verify settings load correctly via core merge logic
        loaded_settings = kiforge.load_merged_settings(self.test_dir)
        self.assertEqual(loaded_settings['output_dir'], 'custom_out')
        self.assertFalse(loaded_settings['export_gerbers'])
        self.assertTrue(loaded_settings['export_drills'])
        self.assertFalse(loaded_settings['export_pos'])
        self.assertTrue(loaded_settings['export_bom'])
        self.assertFalse(loaded_settings['export_ibom'])

        dialog.Destroy()

    def test_advanced_tab_settings_survive_dialog_reopen(self):
        """
        Regression: on construction, txt_output_dir.SetValue() inside
        update_ui_from_settings() fires EVT_TEXT (unlike every other control's
        SetValue() here, wx.TextCtrl genuinely generates that event
        programmatically). The live-CD-sync handler for that event used to read
        the Advanced tab's controls back via _collect_export_params() and
        overwrite self.settings["export_params"] with them -- before that same
        method had populated those controls from the loaded settings, so it
        captured their just-constructed defaults ("both"/unchecked) and clobbered
        the real saved values before the user ever saw them restored.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.choice_pos_side.SetSelection(1)  # Front
            dialog.chk_pos_smd_only.SetValue(True)
            dialog.chk_pos_exclude_dnp.SetValue(False)
            dialog.chk_bom_mfr_mpn.SetValue(False)

            curr = dialog._current_settings()
            kiforge.save_settings(curr, project_dir=self.test_dir, scope="project")
        finally:
            dialog.Destroy()

        # Simulate closing and reopening Studio: a brand new dialog instance,
        # loading whatever was just persisted.
        reopened = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            self.assertEqual(reopened.choice_pos_side.GetSelection(), 1)
            self.assertTrue(reopened.chk_pos_smd_only.IsChecked())
            self.assertFalse(reopened.chk_pos_exclude_dnp.IsChecked())
            self.assertFalse(reopened.chk_bom_mfr_mpn.IsChecked())
        finally:
            reopened.Destroy()

    def test_cd_generation(self):
        """Verify CD workflow and gitignore logic works correctly."""
        # Create a mock .gitignore
        gitignore_path = os.path.join(self.test_dir, ".gitignore")
        with open(gitignore_path, 'w', encoding='utf-8') as f:
            f.write("*.log\n")
            
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        
        # Populate dialog controls
        dialog.txt_project_dir.SetValue(self.test_dir)
        dialog.txt_output_dir.SetValue("kiforge_ci_test")
        dialog.chk_3d.SetValue(False)
        dialog.chk_bom.SetValue(True)
        
        # Simulate CD generation trigger
        class MockEvent:
            pass
        dialog.on_generate_cd(MockEvent())
        
        # Verify files are created
        workflow_path = os.path.join(self.test_dir, ".github", "workflows", "release.yml")
        self.assertTrue(os.path.isfile(workflow_path))
        
        # Read workflow to verify flags
        with open(workflow_path, 'r', encoding='utf-8') as f:
            yaml_content = f.read()
            self.assertIn("output_dir: 'kiforge_ci_test'", yaml_content)
            self.assertIn("export_3d: 'false'", yaml_content)
            self.assertIn("export_bom: 'true'", yaml_content)
            
        # Verify gitignore has been updated
        with open(gitignore_path, 'r', encoding='utf-8') as f:
            git_content = f.read()
            self.assertIn("kiforge_ci_test/", git_content)
            self.assertIn("production/", git_content)
            self.assertIn(".history/", git_content)

        dialog.Destroy()

    def test_save_project_defaults(self):
        """Verify project defaults are saved via the studio dialog handler."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.txt_project_dir.SetValue(self.test_dir)
        dialog.txt_output_dir.SetValue("saved_out")
        dialog.settings["format_jlc"] = False
        dialog.settings.setdefault("exports", {})["format_jlc"] = False

        class MockEvent:
            pass

        dialog.on_save_project_defaults(MockEvent())
        loaded = kiforge.load_merged_settings(self.test_dir)
        self.assertEqual(loaded["output_dir"], "saved_out")
        self.assertFalse(loaded["format_jlc"])
        project_path = kiforge.get_project_settings_path(self.test_dir)
        with open(project_path, "r", encoding="utf-8") as f:
            import json
            saved = json.load(f)
        self.assertIn("exports", saved)
        self.assertFalse(saved["exports"]["format_jlc"])
        dialog.Destroy()

    def test_save_global_defaults(self):
        """Verify global defaults are saved via the studio dialog handler."""
        global_path = kiforge.get_global_settings_path()
        backup = None
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as f:
                backup = f.read()

        try:
            dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
            dialog.chk_generate_cd.SetValue(False)

            class MockEvent:
                pass

            dialog.on_save_global_defaults(MockEvent())
            loaded = kiforge.load_merged_settings(None)
            self.assertFalse(loaded["generate_cd"])
            dialog.Destroy()
        finally:
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

    def test_load_global_defaults_button(self):
        """Verify Load Global Config applies global settings to the dialog."""
        global_path = kiforge.get_global_settings_path()
        backup = None
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as f:
                backup = f.read()

        try:
            kiforge.save_settings(
                {**kiforge.DEFAULT_SETTINGS, "export_3d": False},
                scope="global",
            )
            dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
            dialog.chk_3d.SetValue(True)

            class MockEvent:
                pass

            dialog.on_load_global_defaults(MockEvent())
            self.assertFalse(dialog.chk_3d.IsChecked())
            dialog.Destroy()
        finally:
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

    def test_reset_defaults_button(self):
        """Verify Reset restores built-in defaults in the dialog."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.chk_3d.SetValue(False)

        class MockEvent:
            pass

        dialog.on_reset_defaults(MockEvent())
        self.assertTrue(dialog.chk_3d.IsChecked())
        dialog.Destroy()

    def test_live_cd_sync_on_toggle(self):
        """Verify changing an export checkbox triggers debounced CD workflow sync."""
        from unittest.mock import patch

        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.txt_project_dir.SetValue(self.test_dir)
        dialog.txt_output_dir.SetValue("kiforge_sync_test")
        dialog.chk_3d.SetValue(True)

        class MockEvent:
            pass

        with patch.object(kiforge_studio.kiforge, "generate_cd_files", return_value=("ok", True)) as mock_cd:
            dialog.on_export_setting_changed(MockEvent())
            dialog.on_cd_sync_timer(MockEvent())
            mock_cd.assert_called_once()
            self.assertTrue(mock_cd.call_args[0][2]["export_3d"])
            dialog.chk_3d.SetValue(False)
            dialog.on_export_setting_changed(MockEvent())
            dialog.on_cd_sync_timer(MockEvent())
            self.assertEqual(mock_cd.call_count, 2)
            self.assertFalse(mock_cd.call_args[0][2]["export_3d"])
        dialog.Destroy()

    def test_gerber_toggle_forces_drills(self):
        """Verify enabling gerbers disables and checks the drill checkbox."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.chk_gerbers.SetValue(True)
        dialog._sync_drill_checkbox_state()
        self.assertTrue(dialog.chk_drills.IsChecked())
        self.assertFalse(dialog.chk_drills.IsEnabled())
        dialog.chk_gerbers.SetValue(False)
        dialog._sync_drill_checkbox_state()
        self.assertTrue(dialog.chk_drills.IsEnabled())
        dialog.Destroy()

    def test_homebrew_pdf_toggle_forces_copper_svg(self):
        """Verify enabling Homebrew PDF disables and checks the Copper SVG checkbox.

        Regression test: this dependency existed, was accidentally dropped in
        607adbc while removing an unrelated focus-rectangle workaround (logged
        as "drop unneeded SVG/PDF toggle coupling"), and was restored after
        being reported as broken -- Homebrew PDF is generated from the Copper
        SVG layers, so it must never be exportable with SVG left off.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.chk_homebrew_pdf.SetValue(True)
        dialog._sync_svg_pdf_checkbox_state()
        self.assertTrue(dialog.chk_svg.IsChecked())
        self.assertFalse(dialog.chk_svg.IsEnabled())
        dialog.chk_homebrew_pdf.SetValue(False)
        dialog._sync_svg_pdf_checkbox_state()
        self.assertTrue(dialog.chk_svg.IsEnabled())
        dialog.Destroy()

    def test_deselected_radio_drops_its_accent_ring(self):
        """
        Regression: the previously selected radio kept an orange ring.

        Its dot cleared correctly, but the ring did not. Both custom controls
        used to bind one handler to EVT_SET_FOCUS and EVT_KILL_FOCUS and read
        the state back with HasFocus() -- which, inside a kill-focus handler,
        can still report True because the transfer has not completed. The
        control that just lost focus stayed permanently "focused", and the
        paint code draws an accent ring for a focused glyph.
        """
        frame = wx.Frame(None)
        try:
            group = []
            first = kiforge_studio._FlatRadioButton(frame, label="Documentation", group=group)
            second = kiforge_studio._FlatRadioButton(frame, label="JLCPCB", group=group)

            class Event:
                def Skip(self):
                    pass

            first._apply_selection()
            first._on_set_focus(Event())
            self.assertTrue(first._selected)
            self.assertTrue(first._has_focus)

            # Select the other one: focus leaves `first` while HasFocus() would
            # still report True for it.
            first._on_kill_focus(Event())
            second._on_set_focus(Event())
            second._apply_selection()

            self.assertFalse(first._selected, "dot should clear")
            self.assertFalse(
                first._has_focus,
                "focus flag must come from the event, not a mid-transfer HasFocus()",
            )
            self.assertFalse(
                first._hover or first._has_focus or first._selected,
                "nothing should still be forcing an accent ring on the deselected radio",
            )
        finally:
            frame.Destroy()

    def _glyph_edge_colour(self, control):
        """
        Drive a custom control's real _on_paint and sample its glyph outline.

        Returns the leftmost inked pixel on the glyph's centre row, so the
        same helper works for the checkbox's rounded rect (flush at x=0) and
        the radio's inset circle.
        """
        size = control.GetSize()
        bitmap = wx.Bitmap(size)
        dc = wx.MemoryDC(bitmap)
        # The control clears to its parent's colour, so the ground this
        # compares against has to be that colour and not the palette default,
        # or the very first column reads as ink on any other parent.
        parent = control.GetParent()
        background = parent.GetBackgroundColour() if parent else kiforge_studio._COLORS["app_bg"]
        dc.SetBackground(wx.Brush(background))
        dc.Clear()
        real_dc = wx.AutoBufferedPaintDC
        wx.AutoBufferedPaintDC = lambda _win: dc
        try:
            control._on_paint(None)
        finally:
            wx.AutoBufferedPaintDC = real_dc
        dc.SelectObject(wx.NullBitmap)

        image = bitmap.ConvertToImage()
        row = size[1] // 2
        ground = (background.Red(), background.Green(), background.Blue())
        for column in range(kiforge_studio._CHECKBOX_GLYPH_SIZE):
            pixel = (image.GetRed(column, row), image.GetGreen(column, row), image.GetBlue(column, row))
            if max(abs(a - b) for a, b in zip(pixel, ground)) > 12:
                return pixel
        return ground

    def _is_accent(self, pixel):
        accent = kiforge_studio._COLORS["accent"]
        return all(abs(a - b) < 30 for a, b in zip(pixel, (accent.Red(), accent.Green(), accent.Blue())))

    def test_a_click_takes_focus_without_painting_a_focus_ring(self):
        """
        Regression: every clicked control kept an orange ring afterwards.

        Clearing the ring when focus *left* was only half of it -- a control
        that still holds focus was still drawing one, so the last thing
        clicked on any tab stayed ringed. The platform's own answer is
        focus-visible: click a control on macOS and no ring appears, Tab to it
        and one does. Measured through the controls' own paint code rather
        than asserted on the flag, because the flag is not what the user sees.
        """
        frame = wx.Frame(None)
        try:
            for factory in (kiforge_studio._FlatCheckBox, kiforge_studio._FlatRadioButton):
                control = factory(frame, label="Gerbers")
                control.SetSize((160, 24))
                # Isolate the flag from real focus delivery: whether a hidden
                # frame's child can actually take focus varies by platform and
                # is not what this is testing.
                control.SetFocus = lambda: None

                press = wx.MouseEvent(wx.wxEVT_LEFT_DOWN)
                press.SetPosition(wx.Point(8, 12))
                control._on_left_down(press)
                if control.HasCapture():
                    control.ReleaseMouse()
                control._pressed = False
                self.assertTrue(control._focus_from_pointer, factory.__name__)

                control._on_set_focus(wx.FocusEvent(wx.wxEVT_SET_FOCUS))
                control._hover = False
                self.assertFalse(
                    self._is_accent(self._glyph_edge_colour(control)),
                    f"{factory.__name__} still paints a focus ring after a click",
                )

                control._on_kill_focus(wx.FocusEvent(wx.wxEVT_KILL_FOCUS))
                control._on_set_focus(wx.FocusEvent(wx.wxEVT_SET_FOCUS))
                self.assertTrue(
                    self._is_accent(self._glyph_edge_colour(control)),
                    f"{factory.__name__} gives keyboard focus no visible position",
                )
        finally:
            frame.Destroy()

    def test_checked_glyph_still_shows_keyboard_focus(self):
        """A checked box is filled with the accent, so an accent focus border is invisible."""
        frame = wx.Frame(None)
        try:
            control = kiforge_studio._FlatCheckBox(frame, label="Gerbers")
            control.SetSize((160, 24))
            control.SetValue(True)
            idle = self._glyph_edge_colour(control)
            control._on_set_focus(wx.FocusEvent(wx.wxEVT_SET_FOCUS))
            focused = self._glyph_edge_colour(control)
            self.assertNotEqual(idle, focused, "keyboard focus is invisible on a checked box")
        finally:
            frame.Destroy()

    def test_background_click_hands_focus_to_the_container(self):
        """
        Clicking blank background must move focus off a custom-painted control,
        the way clicking elsewhere does for a native one. _FlatCheckBox and
        _FlatRadioButton paint themselves, and blank panel background claims no
        focus, so without _clear_focus_on_background_click the accent highlight
        stays lit until something else explicitly steals focus.

        Asserted through the container's own focus call rather than
        ``HasFocus()``: real platform focus needs the application to be
        frontmost, which it never is under a test runner -- not even a native
        wx.TextCtrl can hold focus there, so a HasFocus() assertion would fail
        for a reason that has nothing to do with this behaviour.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.notebook.SetSelection(0)
            radio = dialog._preset_radios[0]
            scroll = radio.GetParent()

            claimed = []
            scroll.SetFocusIgnoringChildren = lambda: claimed.append(True)

            evt = wx.MouseEvent(wx.wxEVT_LEFT_DOWN)
            evt.SetEventObject(scroll)
            scroll.ProcessWindowEvent(evt)

            self.assertTrue(
                claimed,
                "clicking the background did not hand focus to the container",
            )
        finally:
            dialog.Destroy()

    def test_background_click_clears_custom_control_focus_end_to_end(self):
        """The same behaviour against real platform focus, when it is obtainable."""
        probe = wx.Frame(None)
        probe.Show()
        native = wx.TextCtrl(probe)
        native.SetFocus()
        can_focus = native.HasFocus()
        probe.Destroy()
        if not can_focus:
            self.skipTest(
                "application is not frontmost; not even a native control can "
                "hold focus, so real-focus behaviour is untestable here"
            )

        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.notebook.SetSelection(0)
            radio = dialog._preset_radios[0]
            radio.SetFocus()
            self.assertTrue(radio.HasFocus())

            scroll = radio.GetParent()
            evt = wx.MouseEvent(wx.wxEVT_LEFT_DOWN)
            evt.SetEventObject(scroll)
            scroll.ProcessWindowEvent(evt)

            self.assertFalse(radio.HasFocus())
        finally:
            dialog.Destroy()

    def test_checkbox_paints_complete_without_graphics_context(self):
        """_FlatCheckBox must render a checked, focused state correctly even
        when wx.GraphicsContext.Create() returns None -- which it reliably
        does on a freshly-created window's very first paint, before it has a
        realized native drawing surface, and reliably does not on any later
        repaint. That GC/plain-DC split is what made a checked box paint as
        an empty square on initial dialog load and only "fix itself" after
        any click triggered a second, GC-backed repaint.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.chk_svg.SetValue(True)
        dialog.chk_svg._has_focus = True
        with patch.object(wx.GraphicsContext, "Create", return_value=None):
            dialog.chk_svg.Refresh()
            dialog.chk_svg.Update()
        dialog.Destroy()

    def test_progress_gauge_is_determinate_and_advances_within_a_task(self):
        """The gauge reports real position, never an indeterminate animation:
        repeated identical reports must not move it, and a long task keeps it
        advancing by reporting sub-progress through report_progress()."""
        dlg = kiforge_studio._ExportProgressDialog(None)
        try:
            seen = []
            with patch.object(dlg.gauge, "SetValue", lambda v: seen.append(v)),                  patch.object(dlg.gauge, "Pulse",
                              lambda: self.fail("gauge must stay determinate")):
                dlg.update(40, "Running: Exporting Homebrew PDF...")
                dlg.update(40, "Running: Exporting Homebrew PDF...")
                self.assertEqual(seen, [40], "an unchanged report must not move the bar")
                dlg.update(46, "Exporting Homebrew PDF: building A4 sheet...")
                dlg.update(52, "Exporting Homebrew PDF: rendering at 1200 DPI...")
                self.assertEqual(seen, [40, 46, 52])
            self.assertEqual(dlg.lbl_message.GetLabel(),
                             "Exporting Homebrew PDF: rendering at 1200 DPI...")
        finally:
            dlg.Destroy()

    def test_sub_task_progress_maps_into_that_tasks_slice(self):
        """report_progress() must map 0-1 into the running task's own slice of
        the overall bar, so sub-progress can never run backwards or overtake
        the next task."""
        reports = []
        ctx = kiforge.ExportContext.__new__(kiforge.ExportContext)
        ctx.progress_callback = lambda i, t, m: reports.append((i, t, m))
        ctx.begin_step(3, 10)
        ctx.report_progress(0.0, "start")
        ctx.report_progress(0.5, "half")
        ctx.report_progress(1.0, "done")
        ctx.report_progress(9.9, "clamped")
        self.assertEqual([r[0] for r in reports], [3.0, 3.5, 4.0, 4.0])
        self.assertTrue(all(r[1] == 10 for r in reports))

    def test_message_dialog_layout_rhythm(self):
        """Message dialog keeps uniform margins with a wider action gap, and
        anchors the glyph to the first text line once the message wraps."""
        def parts(dlg):
            # Locate by type, never by index: the severity glyph is a Material
            # Symbol from the shared icon pipeline, so it is legitimately
            # absent when the icon cannot be fetched or read from cache (a
            # fresh or offline machine), which would shift positional indexes.
            icon = next((c for c in dlg.GetChildren()
                         if isinstance(c, wx.StaticBitmap)), None)
            text = next(c for c in dlg.GetChildren() if isinstance(c, wx.StaticText))
            buttons = [c for c in dlg.GetChildren()
                       if isinstance(c, kiforge_studio._FlatButton)]
            return icon, text, buttons

        short = kiforge_studio._KiForgeMessageDialog(
            None, "Export cancelled.", "KiForge", "cancelled", "ok")
        icon, text, buttons = parts(short)
        button = buttons[-1]
        cw, ch = short.GetClientSize()
        pad, gap = kiforge_studio._SP_LG, kiforge_studio._SP_XL

        # uniform container margins on every side
        self.assertEqual(cw - (text.GetPosition().x + text.GetSize().width), pad)
        self.assertEqual(ch - (button.GetPosition().y + button.GetSize().height), pad)
        self.assertEqual(min(c.GetPosition().x for c in short.GetChildren()), pad)

        content_bottom = max(c.GetPosition().y + c.GetSize().height
                             for c in (icon, text) if c is not None)
        # action row separated by the larger step, not the plain margin
        self.assertEqual(button.GetPosition().y - content_bottom, gap)

        if icon is not None:
            self.assertEqual(icon.GetPosition().y, pad)
            # single line: glyph centred against the text
            self.assertEqual(
                icon.GetPosition().y + icon.GetSize().height // 2,
                text.GetPosition().y + text.GetSize().height // 2,
            )
            # glyph -> text gap is the same container step
            self.assertEqual(
                text.GetPosition().x - (icon.GetPosition().x + icon.GetSize().width), pad)
        short.Destroy()

        wrapped = kiforge_studio._KiForgeMessageDialog(
            None,
            "Export finished with 2 warnings. Some 3D models could not be "
            "resolved and were skipped.",
            "KiForge", "warning", "ok")
        w_icon, w_text, _ = parts(wrapped)
        self.assertGreater(w_text.GetSize().height, w_text.GetCharHeight())
        if w_icon is not None:
            # wrapped: glyph anchored to the first line, not the block centre
            self.assertEqual(w_icon.GetPosition().y, w_text.GetPosition().y)
        wrapped.Destroy()

    def test_custom_preset_opens_advanced_tab(self):
        """Choosing Custom switches to the Advanced tab for individual outputs."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        labels = [pid for pid, _ in kiforge_studio.EXPORT_PRESET_CHOICES]
        custom_idx = labels.index("custom")
        dialog._preset_radios[custom_idx].SetValue(True)
        dialog.on_preset_changed(None)
        self.assertEqual(dialog.notebook.GetSelection(), 1)
        dialog.Destroy()

    def test_jlcpcb_preset_sets_outputs(self):
        """Quick preset applies the expected export toggles."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog._apply_export_preset("jlcpcb")
        self.assertTrue(dialog.chk_gerbers.IsChecked())
        self.assertTrue(dialog.chk_bom.IsChecked())
        self.assertFalse(dialog.chk_ibom.IsChecked())
        self.assertFalse(dialog.chk_sch_pdf.IsChecked())
        self.assertTrue(dialog._export_setting("format_jlc"))
        dialog.Destroy()

    def test_export_pdf_marshals_gui_tier_off_main_thread(self):
        """
        Regression: Studio always renders the homebrew PDF from its export
        worker thread. Qt/wx must never construct their application objects
        off the GUI thread (Cocoa aborts the process for this on macOS), so
        export_svg_to_1200dpi_pdf must marshal onto the wx main thread via
        wx.CallAfter and still return the correct result to the caller.

        Needs a renderer tier that can actually produce a PDF; with none
        installed there is nothing to marshal and the failure would be about
        the environment, not about threading.
        """
        if kiforge.missing_pdf_renderer_packages() and not shutil.which("rsvg-convert"):
            self.skipTest("no PDF renderer available (Pillow/rsvg-convert all missing)")

        front_svg = os.path.join(self.test_dir, "f.svg")
        back_svg = os.path.join(self.test_dir, "b.svg")
        merged_svg = os.path.join(self.test_dir, "m.svg")
        pdf_path = os.path.join(self.test_dir, "m.pdf")
        markup = (
            '<svg width="30mm" height="20mm" viewBox="0 0 30 20">'
            '<rect width="30" height="20" fill="black" /></svg>'
        )
        for path in (front_svg, back_svg):
            with open(path, "w", encoding="utf-8") as f:
                f.write(markup)
        self.assertTrue(kiforge.generate_a4_merged_svg(front_svg, back_svg, merged_svg, "t"))

        result = {}

        def worker():
            result["ran_on_main_thread"] = threading.current_thread() is threading.main_thread()
            result["ok"] = kiforge.export_svg_to_1200dpi_pdf(merged_svg, pdf_path)

        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        # Pump the wx event loop from the main thread so the wx.CallAfter the
        # worker is blocked on actually gets a chance to run.
        deadline = time.time() + 15
        while worker_thread.is_alive() and time.time() < deadline:
            wx.Yield()
            time.sleep(0.01)
        worker_thread.join(timeout=5)

        self.assertFalse(worker_thread.is_alive(), "export worker did not finish; GUI marshaling likely hung")
        self.assertFalse(result.get("ran_on_main_thread"))
        self.assertTrue(result.get("ok"))
        self.assertTrue(os.path.isfile(pdf_path))
        self.assertGreater(os.path.getsize(pdf_path), 0)

    def test_opens_without_a_scrollbar_but_still_scrolls_when_shrunk(self):
        """
        The dialog must open tall enough to show each tab's content, so no
        scrollbar sits beside content that would have fitted. The tabs stay
        scrollable for a small screen or a deliberately shrunk window.

        Height is derived from the tallest tab rather than assumed, so adding
        a control to a tab cannot silently reintroduce an opening scrollbar.
        """
        def scrolls(dialog, page_index):
            page = dialog.notebook.GetPage(page_index)
            return [c for c in page.GetChildren() if isinstance(c, wx.ScrolledWindow)]

        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.Show()
            for _ in range(4):
                wx.Yield()

            size = dialog.GetSize()
            self.assertEqual((size.width % 4, size.height % 4), (0, 0))

            for index in range(dialog.notebook.GetPageCount()):
                dialog.notebook.SetSelection(index)
                for _ in range(4):
                    wx.Yield()
                for scroll in scrolls(dialog, index):
                    self.assertLessEqual(
                        scroll.GetVirtualSize().height,
                        scroll.GetClientSize().height,
                        f"tab {dialog.notebook.GetPageText(index)!r} opens scrolled")

            # shrinking must still scroll rather than clip
            dialog.SetSize((440, 320))
            dialog.SendSizeEvent()
            dialog.notebook.SetSelection(1)
            for _ in range(4):
                wx.Yield()
            self.assertTrue(
                any(s.GetVirtualSize().height > s.GetClientSize().height
                    for s in scrolls(dialog, 1)),
                "a shrunk window must still scroll its content")
        finally:
            dialog.Destroy()

    def test_interactive_resize_snaps_to_the_grid(self):
        """An interactive resize is snapped onto the same 4pt grid the layout
        is built on, via EVT_SIZING's proposed rectangle so the window is never
        painted off-grid."""
        self.assertEqual(kiforge_studio._snap_to_grid(423), 424)
        self.assertEqual(kiforge_studio._snap_to_grid(421), 420)
        self.assertEqual(kiforge_studio._snap_to_grid(1), kiforge_studio._SP_XS)
        for value in (0, 1, 3, 5, 7, 419, 423, 519, 701):
            self.assertEqual(kiforge_studio._snap_to_grid(value) % 4, 0)

        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            evt = wx.SizeEvent((0, 0))
            evt.SetEventType(wx.wxEVT_SIZING)
            evt.SetRect(wx.Rect(0, 0, 423, 517))
            dialog._on_dialog_sizing(evt)
            rect = evt.GetRect()
            self.assertEqual((rect.width % 4, rect.height % 4), (0, 0))
            self.assertEqual((rect.width, rect.height), (424, 516))
        finally:
            dialog.Destroy()

    def test_export_tab_never_clips_when_resized(self):
        """
        Regression: the export summary is a long single-line StaticText, and a
        StaticText reports its full unwrapped text as its minimum size. The
        sizer honoured that, inflating the scrolled panel's virtual width far
        past the dialog, so the controls beside it -- the Browse button, the
        output folder field -- were laid out off the visible area and clipped.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.Show()
            for key in kiforge_studio._EXPORT_TOGGLE_KEYS:
                getattr(dialog, dialog._export_checkbox_attr(key)).SetValue(True)
            dialog._update_export_summary()
            source = dialog._export_summary_text

            for width in (420, 460, 700, 430, 900, 420):
                dialog.SetSize((width, 520))
                dialog.SendSizeEvent()
                for _ in range(3):
                    wx.Yield()

                scroll = dialog.lbl_export_summary.GetParent()
                client = scroll.GetClientSize().width
                self.assertLessEqual(
                    scroll.GetVirtualSize().width, client,
                    f"content wider than the panel at {width}px means clipping")
                for child in scroll.GetChildren():
                    right = child.GetPosition().x + child.GetSize().width
                    self.assertLessEqual(
                        right, client,
                        f"{child.__class__.__name__} clipped at dialog width {width}")

                # Wrap() rewrites the label in place, so re-wrapping must always
                # start from the source text or the breaks compound.
                shown = dialog.lbl_export_summary.GetLabel()
                self.assertEqual(shown.replace(chr(10), " "), source)
        finally:
            dialog.Destroy()

    def test_progress_dialog_edges_line_up(self):
        """Message, gauge and the action row must share one container margin.

        Regression: the Cancel button supplied its own smaller wx.ALL border
        instead of the action row taking the container margin, so it sat 8px
        further right than the gauge directly above it.
        """
        dlg = kiforge_studio._ExportProgressDialog(None)
        try:
            cw = dlg.GetClientSize().width
            pad = kiforge_studio._SP_LG
            lefts, rights = set(), set()
            for child in dlg.GetChildren():
                x, w = child.GetPosition().x, child.GetSize().width
                rights.add(cw - (x + w))
                lefts.add(x)
            self.assertEqual(rights, {pad}, "right edges must all sit on the container margin")
            # only the button is right-aligned, so lefts legitimately differ --
            # but nothing may start inside the margin
            self.assertGreaterEqual(min(lefts), pad)
        finally:
            dlg.Destroy()

    def test_cancel_keeps_progress_visible_until_worker_stops(self):
        """
        Regression: cancelling tore the progress dialog down immediately, while
        the worker was still unwinding its current step. Studio then looked
        idle -- no progress window -- but Export stayed disabled until the
        worker finally exited, which read as the cancel having done nothing.
        The dialog must stay up showing "Cancelling..." until the worker really
        stops, and Studio must return to idle when it does.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.btn_export.Disable()
            dialog._export_running = True
            dialog._export_close_after_finish = False
            dialog._export_state = {
                'running': True, 'success': False, 'error_msg': None,
                'val': 20, 'msg': 'Running: Exporting Homebrew PDF...', 'cancelled': False,
            }
            dialog._export_context = MagicMock()
            thread = MagicMock()
            thread.is_alive.return_value = True
            dialog._export_thread = thread

            progress = kiforge_studio._ExportProgressDialog(dialog)
            dialog._export_progress = progress
            progress._on_cancel(None)              # user clicks Cancel
            self.assertEqual(progress.lbl_message.GetLabel(), "Cancelling…")

            dialog._poll_export_progress(None)     # worker still running
            self.assertTrue(dialog._export_context.cancel.called)
            self.assertIsNotNone(dialog._export_progress,
                                 "progress dialog must stay up while the worker unwinds")
            self.assertTrue(dialog._export_running)

            # late reports from the unwinding worker must not scroll over it
            progress.update(80, "Running: Packaging Gerbers...")
            self.assertEqual(progress.lbl_message.GetLabel(), "Cancelling…")

            # worker finally exits -> dialog shows "Export cancelled." with OK button
            dialog._export_state['running'] = False
            thread.is_alive.return_value = False
            dialog._poll_export_progress(None)
            wx.Yield()
            self.assertFalse(dialog._export_running)
            self.assertFalse(dialog.btn_export.IsEnabled(),
                             "Export button must stay disabled while result dialog is open")
            self.assertEqual(progress.lbl_message.GetLabel(), "Export cancelled.")
            self.assertFalse(progress.gauge.IsShown(),
                             "Progress bar must not be visible on cancelled export")

            # user clicks OK -> dialog is dismissed and Studio returns to idle
            progress._on_dismiss(None)
            self.assertTrue(dialog.btn_export.IsEnabled(),
                            "Export must be usable again once OK is clicked")
        finally:
            dialog._export_running = False
            dialog.Destroy()

    def test_progress_dialog_runs_its_own_modal_loop(self):
        """
        The progress dialog is modal because Studio is.

        Studio runs under ShowModal(), which on macOS is an application-modal
        Cocoa session: a modeless child opened beneath it is drawn behind
        Studio and receives no mouse events at all, so Cancel and OK did
        nothing and the window kept disappearing behind the one that spawned
        it. Hand-pumping events to keep it alive only traded that for a
        flicker, since wx.SafeYield() disables and re-enables every top-level
        window on each poll tick. This checks the dialog really enters a modal
        loop and that OK is what leaves it.
        """
        progress = kiforge_studio._ExportProgressDialog(None)
        try:
            def finish():
                progress.show_result("Export complete. Saved to /kiforge.")
                wx.CallLater(10, lambda: progress._on_dismiss(None))

            wx.CallLater(10, finish)
            self.assertEqual(progress.ShowModal(), wx.ID_OK)
            self.assertFalse(progress.IsModal())
        finally:
            progress.Destroy()

    def test_destroy_progress_dialog_ends_the_loop_rather_than_deleting_it(self):
        """A window cannot be deleted from inside the event loop it is running."""
        progress = MagicMock()
        progress.IsModal.return_value = True
        kiforge_studio._destroy_progress_dialog(progress)
        progress.EndModal.assert_called_once()
        progress.Destroy.assert_not_called()

    def test_escape_during_an_export_cancels_instead_of_closing(self):
        """Closing the window mid-export would leave the worker running unreported."""
        progress = kiforge_studio._ExportProgressDialog(None)
        try:
            event = wx.CloseEvent(wx.wxEVT_CLOSE_WINDOW)
            event.SetCanVeto(True)
            progress._on_close_request(event)
            self.assertTrue(progress.was_cancelled())
            self.assertTrue(event.GetVeto())
        finally:
            progress.Destroy()

    def test_progress_dialog_cancel_does_not_close_studio(self):
        """
        Regression: cancelling from the export progress dialog must only stop
        the export, not close the whole Studio window. Only an explicit close
        (title bar / Close button) while exporting should mark the window to
        close once the export finishes.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.btn_export.Disable()
            dialog._export_running = True
            dialog._export_close_after_finish = False
            dialog._export_state = {
                'running': True, 'success': False, 'error_msg': None,
                'val': 0, 'msg': '', 'cancelled': False,
            }
            dialog._export_context = MagicMock()
            dialog._export_thread = MagicMock()
            dialog._export_thread.is_alive.return_value = False

            fake_progress = MagicMock()
            fake_progress.was_cancelled.return_value = True
            dialog._export_progress = fake_progress

            dialog._poll_export_progress(None)
            wx.Yield()

            self.assertTrue(dialog._export_context.cancel.called)
            self.assertFalse(dialog._export_close_after_finish)
        finally:
            dialog.Destroy()

    def test_explicit_close_while_exporting_marks_close_after_finish(self):
        """The Close button, unlike progress-dialog Cancel, must still close Studio."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
            dialog.btn_export.Disable()
            dialog._export_running = True
            dialog._export_close_after_finish = False
            dialog._export_state = {'cancelled': False}
            dialog._export_context = MagicMock()

            class FakeEvent:
                def Skip(self):
                    pass

            # setUpClass stubs _message_box to return wx.OK for every prompt;
            # this handler only proceeds on wx.YES ("confirm close"), so force
            # that answer for this one call.
            original_message_box = kiforge_studio._message_box
            kiforge_studio._message_box = lambda *args, **kwargs: wx.YES
            try:
                dialog.on_close(FakeEvent())
            finally:
                kiforge_studio._message_box = original_message_box

            self.assertTrue(dialog._export_close_after_finish)
        finally:
            dialog.Destroy()

    def test_dialog_accepts_pcb_file(self):
        pcb_file = os.path.join(self.test_dir, "history_board.kicad_pcb")
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir, pcb_file=pcb_file)
        self.assertEqual(dialog.pcb_file, pcb_file)
        dialog.Destroy()

    def test_dialog_launches_background_dependency_check(self):
        with patch.object(kiforge_studio.KiForgeStudioSettingsDialog, "_check_dependencies_async") as mock_check:
            dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
            mock_check.assert_called_once()
            dialog.Destroy()


class TestStudioPalette(unittest.TestCase):
    """
    Both ramps must be readable, not merely structurally matched.

    The structural checks (matching keys, in-place swap) run everywhere via
    tests/kicad_runtime_stub.py. These need real ``wx.Colour`` values, so they
    live behind the GUI gate with the rest of the wx-dependent tests.
    """

    _WCAG_BODY = 4.5      # normal text
    _WCAG_SECONDARY = 3.0  # muted/secondary text and UI edges

    @staticmethod
    def _luminance(colour):
        """WCAG 2.1 relative luminance of a wx.Colour."""
        def channel(value):
            v = value / 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        return (
            0.2126 * channel(colour.Red())
            + 0.7152 * channel(colour.Green())
            + 0.0722 * channel(colour.Blue())
        )

    @classmethod
    def _contrast(cls, fg, bg):
        light, dark = sorted((cls._luminance(fg), cls._luminance(bg)), reverse=True)
        return (light + 0.05) / (dark + 0.05)

    def _ramps(self):
        return (
            ("dark", kiforge_studio._DARK_PALETTE),
            ("light", kiforge_studio._LIGHT_PALETTE),
        )

    def test_body_text_is_readable_on_every_ground(self):
        """The reported bug was text at one theme's colour on the other's ground."""
        for mode, palette in self._ramps():
            for ground in ("app_bg", "surface", "footer_bg"):
                with self.subTest(mode=mode, ground=ground):
                    ratio = self._contrast(palette["text"], palette[ground])
                    self.assertGreaterEqual(
                        ratio, self._WCAG_BODY,
                        f"{mode} text on {ground} is {ratio:.1f}:1",
                    )
            with self.subTest(mode=mode, ground="input_bg"):
                ratio = self._contrast(palette["input_fg"], palette["input_bg"])
                self.assertGreaterEqual(ratio, self._WCAG_BODY, f"{mode} input {ratio:.1f}:1")

    def test_muted_text_and_accent_stay_legible(self):
        for mode, palette in self._ramps():
            for key in ("muted", "accent"):
                with self.subTest(mode=mode, colour=key):
                    ratio = self._contrast(palette[key], palette["app_bg"])
                    self.assertGreaterEqual(
                        ratio, self._WCAG_SECONDARY,
                        f"{mode} {key} on app_bg is {ratio:.1f}:1",
                    )

    def test_the_two_ramps_actually_run_opposite(self):
        """Guards against a light ramp that was copied from the dark one."""
        dark_bg = self._luminance(kiforge_studio._DARK_PALETTE["app_bg"])
        light_bg = self._luminance(kiforge_studio._LIGHT_PALETTE["app_bg"])
        self.assertLess(dark_bg, 0.1, "dark ground is not dark")
        self.assertGreater(light_bg, 0.7, "light ground is not light")

    def test_appearance_falls_back_to_luminance_without_is_dark(self):
        """
        Not every backend implements SystemAppearance.IsDark.

        wx reports it on Cocoa, GTK and MSW from 4.1, but older builds and some
        GTK themes raise. The fallback reads SYS_COLOUR_WINDOW -- the colour the
        OS will actually paint native controls with -- so the palette still
        matches its surroundings rather than defaulting blindly.
        """
        from unittest.mock import patch

        class NoIsDark:
            @staticmethod
            def GetAppearance():
                raise NotImplementedError("backend has no SystemAppearance")

            @staticmethod
            def GetColour(_which):
                return wx.Colour(250, 250, 250)  # a light GTK theme

        with patch.object(kiforge_studio.wx, "SystemSettings", NoIsDark):
            self.assertFalse(kiforge_studio._system_is_dark())

        class NoIsDarkButDark(NoIsDark):
            @staticmethod
            def GetColour(_which):
                return wx.Colour(30, 30, 32)

        with patch.object(kiforge_studio.wx, "SystemSettings", NoIsDarkButDark):
            self.assertTrue(kiforge_studio._system_is_dark())

    def test_appearance_defaults_to_dark_when_nothing_answers(self):
        """A headless or half-initialised wx must not break dialog construction."""
        from unittest.mock import patch

        class Broken:
            @staticmethod
            def GetAppearance():
                raise RuntimeError("no display")

            @staticmethod
            def GetColour(_which):
                raise RuntimeError("no display")

        with patch.object(kiforge_studio.wx, "SystemSettings", Broken):
            self.assertTrue(kiforge_studio._system_is_dark())

    def test_refresh_palette_follows_the_system_appearance(self):
        mode = kiforge_studio.refresh_palette()
        self.assertIn(mode, ("dark", "light"))
        self.assertEqual(kiforge_studio.active_palette_mode(), mode)
        expected = (
            kiforge_studio._DARK_PALETTE if mode == "dark" else kiforge_studio._LIGHT_PALETTE
        )
        self.assertEqual(kiforge_studio._COLORS["app_bg"], expected["app_bg"])


if __name__ == '__main__':
    unittest.main()
