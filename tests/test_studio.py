import unittest
import sys
import os
import tempfile
import json
import shutil

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
            
        # Verify settings load correctly
        loaded_settings = dialog.load_settings(self.test_dir)
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
            self.assertIn(".history/", git_content)

        dialog.Destroy()

    def test_save_project_defaults(self):
        """Verify project defaults are saved via the studio dialog handler."""
        dialog = kiforge_studio.KiForgeStudioSettingsDialog(None, self.test_dir)
        dialog.txt_project_dir.SetValue(self.test_dir)
        dialog.txt_output_dir.SetValue("saved_out")
        dialog.chk_format_jlc.SetValue(False)

        class MockEvent:
            pass

        dialog.on_save_project_defaults(MockEvent())
        loaded = kiforge.load_merged_settings(self.test_dir)
        self.assertEqual(loaded["output_dir"], "saved_out")
        self.assertFalse(loaded["format_jlc"])
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

if __name__ == '__main__':
    unittest.main()
