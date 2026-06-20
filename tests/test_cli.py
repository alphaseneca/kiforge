import unittest
import sys
import os

# Add root directory to sys.path to import kiforge
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import kiforge

class TestKiForgeCLI(unittest.TestCase):
    def test_default_arguments(self):
        """Verify default values are correctly parsed when no arguments are provided."""
        args = kiforge.parse_cli_args([])
        self.assertEqual(args.project_path, ".")
        self.assertEqual(args.output_dir, "kiforge")
        self.assertTrue(args.export_3d)
        self.assertTrue(args.export_svg)
        self.assertTrue(args.export_bom)
        self.assertTrue(args.export_sch_pdf)
        self.assertTrue(args.export_pos)
        self.assertTrue(args.export_step)
        self.assertTrue(args.export_gerbers)
        self.assertTrue(args.export_drills)
        self.assertTrue(args.export_ibom)
        self.assertTrue(args.format_jlc)
        self.assertFalse(args.generate_cd)

    def test_custom_paths(self):
        """Verify project path and output directory custom arguments."""
        args = kiforge.parse_cli_args(["--project-path", "my_project", "--output-dir", "out"])
        self.assertEqual(args.project_path, "my_project")
        self.assertEqual(args.output_dir, "out")

    def test_no_export_flags(self):
        """Verify negative (no-export) flags correctly set options to False."""
        args = kiforge.parse_cli_args([
            "--no-export-3d",
            "--no-export-svg",
            "--no-export-bom",
            "--no-export-sch-pdf",
            "--no-export-pos",
            "--no-export-step",
            "--no-export-gerbers",
            "--no-export-drills",
            "--no-export-ibom"
        ])
        self.assertFalse(args.export_3d)
        self.assertFalse(args.export_svg)
        self.assertFalse(args.export_bom)
        self.assertFalse(args.export_sch_pdf)
        self.assertFalse(args.export_pos)
        self.assertFalse(args.export_step)
        self.assertFalse(args.export_gerbers)
        self.assertFalse(args.export_drills)
        self.assertFalse(args.export_ibom)

    def test_explicit_export_flags(self):
        """Verify explicit positive flags are parsed as True."""
        args = kiforge.parse_cli_args([
            "--export-3d",
            "--export-svg"
        ])
        self.assertTrue(args.export_3d)
        self.assertTrue(args.export_svg)

    def test_get_kicad_cli_path(self):
        """Verify get_kicad_cli_path returns a string and is not empty."""
        path = kiforge.get_kicad_cli_path()
        self.assertTrue(isinstance(path, str))
        self.assertTrue(len(path) > 0)

    def test_get_kicad_python_path(self):
        """Verify get_kicad_python_path returns a string and is not empty."""
        path = kiforge.get_kicad_python_path()
        self.assertTrue(isinstance(path, str))
        self.assertTrue(len(path) > 0)

    def test_generate_cd_only_flag(self):
        """Verify --generate-cd sets generate_cd mode without running export."""
        args = kiforge.parse_cli_args(["--generate-cd"])
        self.assertTrue(args.generate_cd)

    def test_github_actions_skips_cd_on_export(self):
        """Verify CD workflow generation is skipped when GITHUB_ACTIONS is set."""
        import tempfile
        import shutil
        from unittest.mock import patch

        temp_dir = tempfile.mkdtemp()
        try:
            pcb_path = os.path.join(temp_dir, "board.kicad_pcb")
            with open(pcb_path, "w", encoding="utf-8") as f:
                f.write("(kicad_pcb (version 20240108)\n")

            options = {
                "generate_cd": True,
                "export_gerbers": False,
                "export_drills": False,
                "export_pos": False,
                "export_bom": False,
                "export_ibom": False,
                "export_sch_pdf": False,
                "export_step": False,
                "export_3d": False,
                "export_svg": False,
            }
            context = kiforge.ExportContext(temp_dir, "out", options)
            original_setup_logger = kiforge.setup_logger
            kiforge.setup_logger = lambda dir: None
            try:
                self.assertTrue(context.resolve())
            finally:
                kiforge.setup_logger = original_setup_logger

            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False):
                with patch.object(kiforge, "generate_cd_files") as mock_cd:
                    with patch.object(kiforge.ExportRunner, "execute", return_value=True):
                        kiforge.run_export(context=context)
                        mock_cd.assert_not_called()

            env_backup = os.environ.pop("GITHUB_ACTIONS", None)
            try:
                with patch.object(kiforge, "generate_cd_files", return_value=("CD updated", True)) as mock_cd:
                    with patch.object(kiforge.ExportRunner, "execute", return_value=True):
                        kiforge.run_export(context=context)
                        mock_cd.assert_called_once()
            finally:
                if env_backup is not None:
                    os.environ["GITHUB_ACTIONS"] = env_backup
        finally:
            shutil.rmtree(temp_dir)

    def test_context_cancellation(self):
        """Verify ExportContext thread-safe cancellation functions as expected."""
        options = {"export_bom": True}
        context = kiforge.ExportContext(".", "kiforge_out", options)
        self.assertFalse(context.is_aborted())
        context.cancel()
        self.assertTrue(context.is_aborted())

    def test_rotation_offsets_merge(self):
        """Verify ExportContext merges and exposes rotation offsets correctly."""
        options = {
            "rotation_offsets": {"R0603": 90.0, "U1": 180.0}
        }
        context = kiforge.ExportContext(".", "kiforge_out", options)
        # Call resolve, but since '.' might not contain kicad board files, resolve returns False.
        # However, it still merges the rotation offsets. Let's mock the pcb resolution.
        context.pcb_file = "dummy.kicad_pcb"
        context.pcb_name = "dummy"
        context.project_dir = "."
        context.output_dir = "kiforge_out"
        context.temp_gerber_dir = "kiforge_out/temp_gerbers"
        
        # Manually trigger settings loading/merging or test the constructor logic
        # In our refactored ExportContext, the merge happens at the end of resolve().
        # Let's mock resolve's dependency methods or check it directly.
        # We can just manually call the logic or mock setup_logger.
        original_setup_logger = kiforge.setup_logger
        kiforge.setup_logger = lambda dir: None
        try:
            context.resolve()
        finally:
            kiforge.setup_logger = original_setup_logger
            
        self.assertEqual(context.rotation_offsets.get("R0603"), 90.0)
        self.assertEqual(context.rotation_offsets.get("U1"), 180.0)

    def test_cd_generation(self):
        """Verify centralized CD file generation creates correct workflow files and .gitignore entry."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            # Create a mock .gitignore
            gitignore_path = os.path.join(temp_dir, ".gitignore")
            with open(gitignore_path, 'w', encoding='utf-8') as f:
                f.write("*.log\n")
                
            options = {"export_3d": False, "export_bom": True}
            msg, success = kiforge.generate_cd_files(temp_dir, "kiforge_test_ci", options)
            self.assertTrue(success)
            
            # Check GitHub workflow file
            github_workflow_path = os.path.join(temp_dir, ".github", "workflows", "release.yml")
            self.assertTrue(os.path.isfile(github_workflow_path))
            with open(github_workflow_path, 'r', encoding='utf-8') as f:
                content = f.read()
                self.assertIn("output_dir: 'kiforge_test_ci'", content)
                self.assertIn("export_3d: 'false'", content)

            # Check Gitea workflow file
            gitea_workflow_path = os.path.join(temp_dir, ".gitea", "workflows", "release.yml")
            self.assertTrue(os.path.isfile(gitea_workflow_path))
            with open(gitea_workflow_path, 'r', encoding='utf-8') as f:
                gitea_content = f.read()
                self.assertIn("output_dir: 'kiforge_test_ci'", gitea_content)
                self.assertIn("export_3d: 'false'", gitea_content)
                self.assertIn("softprops/action-gh-release@v2", gitea_content)
                
            # Check gitignore
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                git_content = f.read()
                self.assertIn("kiforge_test_ci/", git_content)
                self.assertIn(".history/", git_content)
        finally:
            shutil.rmtree(temp_dir)

    def test_gitignore_template_patterns(self):
        """Verify gitignore template includes KiCad 10 patterns."""
        patterns = kiforge.load_gitignore_patterns("kiforge_out")
        self.assertIn("kiforge_out/", patterns)
        self.assertIn(".history/", patterns)
        self.assertIn("*.kicad_prl", patterns)
        self.assertIn("bom/", patterns)

    def test_global_settings_save_and_merge(self):
        """Verify global settings persist and merge with project settings."""
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        global_path = kiforge.get_global_settings_path()
        backup = None
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as f:
                backup = f.read()

        try:
            kiforge.save_settings({"format_jlc": False, "output_dir": "global_out"}, scope="global")
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertFalse(merged["format_jlc"])
            self.assertEqual(merged["output_dir"], "global_out")

            project_settings = os.path.join(temp_dir, ".kiforge.json")
            with open(project_settings, "w", encoding="utf-8") as f:
                import json
                json.dump({"output_dir": "project_out"}, f)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertEqual(merged["output_dir"], "project_out")
            self.assertFalse(merged["format_jlc"])
        finally:
            shutil.rmtree(temp_dir)
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

    def test_drill_runs_when_only_gerbers_enabled(self):
        """Verify drill export runs when gerbers are enabled even if drills flag is false."""
        task = kiforge.DrillExportTask()
        context = kiforge.ExportContext(".", "kiforge_out", {"export_gerbers": True, "export_drills": False})
        context.pcb_file = "dummy.kicad_pcb"
        self.assertTrue(task.is_applicable(context))

    def test_jlc_format_tasks_respect_format_jlc_flag(self):
        """Verify JLC formatting tasks are skipped when format_jlc is false."""
        bom_task = kiforge.JlcBomFormatTask()
        cpl_task = kiforge.JlcCplFormatTask()
        context = kiforge.ExportContext(".", "kiforge_out", {"format_jlc": False, "export_bom": True, "export_pos": True})
        context.pcb_file = "dummy.kicad_pcb"
        context.sch_file = "dummy.kicad_sch"
        self.assertFalse(bom_task.is_applicable(context))
        self.assertFalse(cpl_task.is_applicable(context))

    def test_version_tag_resolution(self):
        """Verify version resolution and normalization from environment, options, and file extraction."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            # Create a dummy pcb and sch file
            pcb_path = os.path.join(temp_dir, "myboard.kicad_pcb")
            sch_path = os.path.join(temp_dir, "myboard.kicad_sch")
            
            with open(pcb_path, "w", encoding="utf-8") as f:
                f.write('(title_block (rev "1.2.3-pcb"))\n')
            with open(sch_path, "w", encoding="utf-8") as f:
                f.write('(title_block (rev "1.2.3-sch"))\n')
                
            # Case 1: Option version takes priority and normalizes (prepends 'v' if digit)
            options = {"version": "9.9.9"}
            context = kiforge.ExportContext(temp_dir, "out", options)
            # Mock setup_logger to prevent log folder generation
            original_setup_logger = kiforge.setup_logger
            kiforge.setup_logger = lambda dir: None
            try:
                self.assertTrue(context.resolve())
            finally:
                kiforge.setup_logger = original_setup_logger
            self.assertEqual(context.pcb_name, "myboard_v9.9.9")
            
            # Case 2: Extract revision from schematic file
            options = {}
            context = kiforge.ExportContext(temp_dir, "out", options)
            original_setup_logger = kiforge.setup_logger
            kiforge.setup_logger = lambda dir: None
            try:
                self.assertTrue(context.resolve())
            finally:
                kiforge.setup_logger = original_setup_logger
            # Should read "1.2.3-sch" and normalize to "v1.2.3-sch"
            self.assertEqual(context.pcb_name, "myboard_v1.2.3-sch")

            # Case 3: Default to v0.1.0 when no rev, env, or option is available
            plain_dir = tempfile.mkdtemp()
            try:
                plain_pcb = os.path.join(plain_dir, "plain.kicad_pcb")
                with open(plain_pcb, "w", encoding="utf-8") as f:
                    f.write("(kicad_pcb (version 20240108)\n")
                context = kiforge.ExportContext(plain_dir, "out", {})
                kiforge.setup_logger = lambda dir: None
                try:
                    self.assertTrue(context.resolve())
                finally:
                    kiforge.setup_logger = original_setup_logger
                self.assertEqual(context.pcb_name, "plain_v0.1.0")
            finally:
                shutil.rmtree(plain_dir)
            
        finally:
            shutil.rmtree(temp_dir)

    def test_generate_ci_files_alias(self):
        """Verify deprecated generate_ci_files alias still works."""
        self.assertIs(kiforge.generate_ci_files, kiforge.generate_cd_files)

    def test_step3d_export_task_vrml_warning(self):
        """Verify that Step3dExportTask intercepts VRML model export errors, logs a warning, and returns True."""
        task = kiforge.Step3dExportTask()
        
        # Create a mock ExportContext
        options = {"export_step": True}
        context = kiforge.ExportContext(".", "kiforge_out", options)
        context.pcb_file = "dummy.kicad_pcb"
        context.pcb_name = "dummy"
        context.project_dir = "."
        context.output_dir = "kiforge_out"
        context.temp_gerber_dir = "kiforge_out/temp_gerbers"
        
        import logging
        context.logger = logging.getLogger("kiforge_test_vrml")
        
        # Mock _run_subprocess to raise a RuntimeError with the VRML error message
        def mock_run_subprocess(cmd, ctx):
            raise RuntimeError("Command failed: pcb export step ...\n\nError:\nCannot use VRML models when exporting to non-mesh formats.")
            
        task._run_subprocess = mock_run_subprocess
        
        # This should log a warning and return True instead of raising an error/returning False
        with self.assertLogs("kiforge_test_vrml", level="WARNING") as cm:
            result = task.run(context)
            self.assertTrue(result)
            self.assertTrue(any("Cannot use VRML models" in log for log in cm.output))


if __name__ == '__main__':
    unittest.main()
