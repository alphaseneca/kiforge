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

    def test_print_pdf_toggle_forces_copper_svg(self):
        """Verify enabling Homebrew PDF disables and checks the Copper SVG checkbox.

        Regression test: this dependency existed, was accidentally dropped in
        607adbc while removing an unrelated focus-rectangle workaround (logged
        as "drop unneeded SVG/PDF toggle coupling"), and was restored after
        being reported as broken -- Homebrew PDF is generated from the Copper
        SVG layers, so it must never be exportable with SVG left off.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.chk_print_pdf.SetValue(True)
        dialog._sync_svg_pdf_checkbox_state()
        self.assertTrue(dialog.chk_svg.IsChecked())
        self.assertFalse(dialog.chk_svg.IsEnabled())
        dialog.chk_print_pdf.SetValue(False)
        dialog._sync_svg_pdf_checkbox_state()
        self.assertTrue(dialog.chk_svg.IsEnabled())
        dialog.Destroy()

    def test_background_click_clears_custom_control_focus(self):
        """Clicking blank panel background must clear focus from a
        custom-painted checkbox/radio, the same way it would for a native
        control. _FlatCheckBox/_FlatRadioButton own their painting instead of
        wrapping a native widget, so wx never gives them this for free --
        without _dismiss_focus_on_click, a focused control's accent-border
        highlight stayed lit until something else (e.g. the notebook tab
        strip) explicitly stole focus, which is what was reported broken.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.notebook.SetSelection(0)
        radio = dialog._preset_radios[0]
        radio.SetFocus()
        self.assertTrue(radio.HasFocus())

        scroll = radio.GetParent()
        evt = wx.MouseEvent(wx.wxEVT_LEFT_DOWN)
        evt.SetEventObject(scroll)
        scroll.ProcessWindowEvent(evt)

        self.assertFalse(radio.HasFocus())
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
        """
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

            # worker finally exits -> dialog closes and Studio returns to idle
            dialog._export_state['running'] = False
            thread.is_alive.return_value = False
            dialog._poll_export_progress(None)
            wx.Yield()
            self.assertFalse(dialog._export_running)
            self.assertTrue(dialog.btn_export.IsEnabled(),
                            "Export must be usable again once the export stops")
        finally:
            dialog._export_running = False
            dialog.Destroy()

    def test_progress_dialog_cancel_does_not_close_studio(self):
        """
        Regression: cancelling from the export progress dialog must only stop
        the export, not close the whole Studio window. Only an explicit close
        (title bar / Close button) while exporting should mark the window to
        close once the export finishes.
        """
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        try:
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

if __name__ == '__main__':
    unittest.main()
