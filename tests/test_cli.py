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

if __name__ == '__main__':
    unittest.main()
