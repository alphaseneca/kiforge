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
from unittest.mock import MagicMock

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
        # Mock wx.MessageBox to prevent modal dialog popups blocking automated tests
        cls.original_message_box = wx.MessageBox
        wx.MessageBox = lambda *args, **kwargs: wx.OK

    @classmethod
    def tearDownClass(cls):
        # Restore original wx.MessageBox
        wx.MessageBox = cls.original_message_box

    def setUp(self):
        # Create a temporary directory representing a KiCad project
        self.test_dir = tempfile.mkdtemp()
        
    def tearDown(self):
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

            # setUpClass stubs wx.MessageBox to return wx.OK for every prompt;
            # this handler only proceeds on wx.YES ("confirm close"), so force
            # that answer for this one call.
            original_message_box = wx.MessageBox
            wx.MessageBox = lambda *args, **kwargs: wx.YES
            try:
                dialog.on_close(FakeEvent())
            finally:
                wx.MessageBox = original_message_box

            self.assertTrue(dialog._export_close_after_finish)
        finally:
            dialog.Destroy()

if __name__ == '__main__':
    unittest.main()
