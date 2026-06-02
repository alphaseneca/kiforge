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

    def test_ci_generation(self):
        """Verify centralized CI file generation creates correct workflow files and .gitignore entry."""
        import tempfile
        import shutil
        
        temp_dir = tempfile.mkdtemp()
        try:
            # Create a mock .gitignore
            gitignore_path = os.path.join(temp_dir, ".gitignore")
            with open(gitignore_path, 'w', encoding='utf-8') as f:
                f.write("*.log\n")
                
            options = {"export_3d": False, "export_bom": True}
            msg, success = kiforge.generate_ci_files(temp_dir, "kiforge_test_ci", options)
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
                self.assertIn("release-action@v1", gitea_content)
                
            # Check gitignore
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                git_content = f.read()
                self.assertIn("kiforge_test_ci/", git_content)
        finally:
            shutil.rmtree(temp_dir)

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
            
        finally:
            shutil.rmtree(temp_dir)

if __name__ == '__main__':
    unittest.main()
