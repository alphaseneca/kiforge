"""
Unit tests for kiforge.py — CLI parsing, settings merge, versioning, export tasks,
JLC formatting, CD/gitignore generation, and git-tag resolution.

Does not require KiCad to be installed; subprocess export tests skip or mock when
kicad-cli is unavailable.
"""
import unittest
import sys
import os
import subprocess
import shutil
import tempfile

# Add root directory to sys.path to import kiforge
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import kiforge

def _rmtree_force(path: str) -> None:
    """Remove a directory tree, including read-only git objects on Windows."""
    import stat

    def _onerror(func, p, _exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_onerror)


def _without_github_actions_tag_env():
    """
    Ignore GITHUB_REF_NAME on tag-triggered workflows.

    Release CI sets GITHUB_REF_TYPE=tag and GITHUB_REF_NAME=vX.Y.Z; local/git-tag
    resolution tests must not inherit that tag.
    """
    from unittest.mock import patch

    return patch.dict(
        os.environ,
        {"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": ""},
        clear=False,
    )


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

    def test_subprocess_responds_to_cancel(self):
        """Cancelling an in-flight subprocess must not block until the command finishes."""
        import threading
        import time

        context = kiforge.ExportContext(".", "kiforge_out", {})
        context.project_dir = "."
        context.startupinfo = kiforge._subprocess_startupinfo()
        task = kiforge.GerberExportTask()

        def cancel_after_delay():
            time.sleep(0.25)
            context.cancel()

        threading.Thread(target=cancel_after_delay, daemon=True).start()
        started = time.time()
        result = task._run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            context,
        )
        elapsed = time.time() - started
        self.assertFalse(result)
        self.assertTrue(context.is_aborted())
        self.assertLess(elapsed, 8, "subprocess cancellation took too long")

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
                self.assertIn("pos_side: 'both'", content)
                self.assertIn("pos_smd_only: 'true'", content)
                self.assertIn(kiforge.KIFORGE_ACTION_REF, content)
                self.assertIn("softprops/action-gh-release@v2", content)
                self.assertIn("generate_release_notes: true", content)
                self.assertIn("overwrite: true", content)
                self.assertIn("overwrite_files: true", content)

            # Check Gitea workflow file
            gitea_workflow_path = os.path.join(temp_dir, ".gitea", "workflows", "release.yml")
            self.assertTrue(os.path.isfile(gitea_workflow_path))
            with open(gitea_workflow_path, 'r', encoding='utf-8') as f:
                gitea_content = f.read()
                self.assertIn("output_dir: 'kiforge_test_ci'", gitea_content)
                self.assertIn("export_3d: 'false'", gitea_content)
                self.assertIn(
                    f"https://github.com/{kiforge.KIFORGE_ACTION_REF}",
                    gitea_content,
                )
                self.assertIn("softprops/action-gh-release@v2", gitea_content)
                self.assertNotIn("generate_release_notes", gitea_content)
                self.assertIn("overwrite: true", gitea_content)
                self.assertIn("overwrite_files: true", gitea_content)
            # Check gitignore
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                git_content = f.read()
                self.assertIn("kiforge_test_ci/", git_content)
                self.assertIn("production/", git_content)
                self.assertIn(".history/", git_content)
        finally:
            shutil.rmtree(temp_dir)

    def test_gitignore_template_patterns(self):
        """Verify gitignore template includes KiCad patterns."""
        patterns = kiforge.load_gitignore_patterns("kiforge_out")
        self.assertIn("kiforge_out/", patterns)
        self.assertIn("*.kicad_prl", patterns)
        self.assertIn("bom/", patterns)
        self.assertIn("production/", patterns)
        self.assertIn(".history/", patterns)

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
            with open(global_path, "r", encoding="utf-8") as f:
                import json
                saved = json.load(f)
            self.assertIn("exports", saved)
            self.assertFalse(saved["exports"]["format_jlc"])
            self.assertNotIn("export_gerbers", saved)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertFalse(merged["format_jlc"])
            self.assertEqual(merged["output_dir"], "global_out")

            project_settings = os.path.join(temp_dir, ".kiforge.json")
            with open(project_settings, "w", encoding="utf-8") as f:
                import json
                json.dump({"output_dir": "project_out", "exports": {"format_jlc": True}}, f)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertEqual(merged["output_dir"], "project_out")
            self.assertTrue(merged["format_jlc"])
        finally:
            shutil.rmtree(temp_dir)
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

    def test_title_block_rev_helpers(self):
        """Verify title-block rev insertion/update without modifying the source file."""
        import tempfile
        import shutil

        content = '(kicad_sch\n\t(paper "A4")\n\t(generator "eeschema")\n)\n'
        updated = kiforge.update_kicad_file_title_block_rev(content, "v2.0.0")
        self.assertIn('(rev "v2.0.0")', updated)
        self.assertIn('(paper "A4")', updated)

        existing = '(title_block (rev "old") (date "2024-01-01"))\n'
        updated = kiforge.update_kicad_file_title_block_rev(existing, "v3.1.4")
        self.assertIn('(rev "v3.1.4")', updated)
        self.assertIn('(date "2024-01-01")', updated)
        self.assertNotIn("old", updated)

        temp_root = tempfile.mkdtemp()
        sch_path = os.path.join(temp_root, "board.kicad_sch")
        try:
            with open(sch_path, "w", encoding="utf-8") as f:
                f.write(content)
            temp_dir, staged = kiforge.create_title_block_staged_copy(sch_path, "v1.2.3")
            try:
                with open(staged, "r", encoding="utf-8") as f:
                    staged_content = f.read()
                with open(sch_path, "r", encoding="utf-8") as f:
                    original_content = f.read()
                self.assertIn('(rev "v1.2.3")', staged_content)
                self.assertEqual(original_content, content)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            shutil.rmtree(temp_root)

    def test_export_runtime_options_not_persisted(self):
        """Title-block sync is export-runtime only, not saved in settings JSON."""
        import tempfile
        import shutil
        import json

        temp_dir = tempfile.mkdtemp()
        global_path = kiforge.get_global_settings_path()
        backup = None
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as f:
                backup = f.read()

        try:
            runtime = kiforge.apply_export_runtime_options({})
            self.assertTrue(runtime["sync_title_block_rev"])

            kiforge.save_settings({"format_jlc": False}, scope="global")
            with open(global_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn("sync_title_block_rev", saved)
            self.assertNotIn("sync_title_block_rev", saved.get("exports", {}))

            with open(os.path.join(temp_dir, ".kiforge.json"), "w", encoding="utf-8") as f:
                json.dump({"exports": {"sync_title_block_rev": False}}, f)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertNotIn("sync_title_block_rev", merged)
        finally:
            shutil.rmtree(temp_dir)
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

    def test_legacy_flat_export_settings(self):
        """Verify legacy flat export keys in saved JSON still load correctly."""
        import tempfile
        import shutil
        import json

        temp_dir = tempfile.mkdtemp()
        try:
            with open(os.path.join(temp_dir, ".kiforge.json"), "w", encoding="utf-8") as f:
                json.dump({"export_bom": False}, f)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertFalse(merged["export_bom"])
        finally:
            shutil.rmtree(temp_dir)

    def test_drill_runs_when_only_gerbers_enabled(self):
        """Verify drill export runs when gerbers are enabled even if drills flag is false."""
        task = kiforge.DrillExportTask()
        context = kiforge.ExportContext(".", "kiforge_out", {"export_gerbers": True, "export_drills": False})
        context.pcb_file = "dummy.kicad_pcb"
        self.assertTrue(task.is_applicable(context))

    def test_export_params_defaults(self):
        """Default export params align with the standard kicad-cli manufacturing script."""
        merged = kiforge.merge_export_params(None, None)
        self.assertEqual(merged["pos_side"], "both")
        self.assertTrue(merged["pos_smd_only"])
        self.assertTrue(merged["pos_exclude_dnp"])

    def test_bom_export_defaults(self):
        """Raw BOM export uses fixed BOM_EXPORT_DEFAULTS (not export_params)."""
        from unittest.mock import patch

        task = kiforge.BomExportTask()
        context = kiforge.ExportContext(".", "out", {})
        context.sch_file = "board.kicad_sch"
        context.kicad_cli = "kicad-cli"
        context.output_dir = "out"
        captured = {}

        def fake_run(cmd, ctx, **kwargs):
            captured["cmd"] = cmd
            return True

        with patch.object(task, "_run_subprocess", side_effect=fake_run):
            self.assertTrue(task.run(context))
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("--fields") + 1], kiforge.BOM_EXPORT_DEFAULTS["fields"])
        self.assertEqual(cmd[cmd.index("--group-by") + 1], kiforge.BOM_EXPORT_DEFAULTS["group_by"])
        self.assertIn("ID", cmd[cmd.index("--fields") + 1])

    def test_lcsc_part_number_from_id(self):
        """JLC LCSC Part # is copied from ID only when ID matches ^C\\d+$."""
        row = {"ID": "C125111"}
        self.assertEqual(kiforge.JLCPCBFormatter._lcsc_part_number(row), "C125111")
        self.assertEqual(kiforge.JLCPCBFormatter._lcsc_part_number({"ID": "R0603"}), "")
        self.assertEqual(kiforge.JLCPCBFormatter._lcsc_part_number({"ID": "c125111"}), "")
        self.assertEqual(kiforge.JLCPCBFormatter._lcsc_part_number({"ID": "C125111X"}), "")
        self.assertEqual(
            kiforge.JLCPCBFormatter._lcsc_part_number({"LCSC Part #": "C125111"}),
            "",
        )

    def test_resolve_jlc_gerber_layers_two_layer_board(self):
        """JLC gerber export includes only manufacturing layers on a 2-layer board."""
        pcb = os.path.join("tests", "sample_project", "sample.kicad_pcb")
        layers = kiforge.resolve_jlc_gerber_layers(pcb)
        self.assertEqual(
            layers,
            [
                "F.Cu", "B.Cu",
                "F.Paste", "B.Paste", "F.SilkS", "B.SilkS",
                "F.Mask", "B.Mask", "Edge.Cuts",
                "Dwgs.User", "Cmts.User",
            ],
        )
        self.assertNotIn("F.Fab", layers)
        self.assertNotIn("User.1", layers)

    def test_gerber_export_defaults(self):
        """Gerber export uses JLC manufacturing layers and zone refill."""
        from unittest.mock import patch

        task = kiforge.GerberExportTask()
        pcb = os.path.join("tests", "sample_project", "sample.kicad_pcb")
        context = kiforge.ExportContext(".", "out", {})
        context.pcb_file = pcb
        context.kicad_cli = "kicad-cli"
        context.temp_gerber_dir = "out/temp_gerbers"
        captured = {}

        def fake_run(cmd, ctx, **kwargs):
            captured["cmd"] = cmd
            return True

        with patch.object(task, "_run_subprocess", side_effect=fake_run):
            self.assertTrue(task.run(context))
        cmd = captured["cmd"]
        layer_arg = cmd[cmd.index("--layers") + 1]
        self.assertIn("F.Cu", layer_arg)
        self.assertIn("Edge.Cuts", layer_arg)
        self.assertNotIn("F.Fab", layer_arg)
        self.assertIn("--check-zones", cmd)
        self.assertIn("--use-drill-file-origin", cmd)
        self.assertNotIn("--no-protel-ext", cmd)

    def test_drill_export_defaults(self):
        """Drill export matches JLC Excellon settings (merged PTH/NPTH, absolute origin)."""
        from unittest.mock import patch

        task = kiforge.DrillExportTask()
        context = kiforge.ExportContext(".", "out", {})
        context.pcb_file = "board.kicad_pcb"
        context.kicad_cli = "kicad-cli"
        context.temp_gerber_dir = "out/temp_gerbers"
        captured = {}

        def fake_run(cmd, ctx, **kwargs):
            captured["cmd"] = cmd
            return True

        with patch.object(task, "_run_subprocess", side_effect=fake_run):
            self.assertTrue(task.run(context))
        cmd = captured["cmd"]
        self.assertNotIn("--excellon-separate-th", cmd)
        self.assertEqual(cmd[cmd.index("--drill-origin") + 1], "absolute")
        self.assertEqual(cmd[cmd.index("--excellon-units") + 1], "mm")
        self.assertEqual(cmd[cmd.index("--excellon-zeros-format") + 1], "decimal")
        self.assertEqual(cmd[cmd.index("--excellon-oval-format") + 1], "alternate")

    def test_gerber_zip_skips_job_file(self):
        """Gerber ZIP omits KiCad job files not needed by fab houses."""
        import zipfile

        temp_dir = tempfile.mkdtemp()
        try:
            gerber_dir = os.path.join(temp_dir, "temp_gerbers")
            os.makedirs(gerber_dir)
            with open(os.path.join(gerber_dir, "board-F_Cu.gtl"), "w", encoding="utf-8") as f:
                f.write("gerber")
            with open(os.path.join(gerber_dir, "board-job.gbrjob"), "w", encoding="utf-8") as f:
                f.write("job")

            task = kiforge.GerberPackTask()
            context = kiforge.ExportContext(".", temp_dir, {"export_gerbers": True})
            context.pcb_file = "board.kicad_pcb"
            context.pcb_name = "board"
            context.output_dir = temp_dir
            context.temp_gerber_dir = gerber_dir
            self.assertTrue(task.run(context))

            zip_path = os.path.join(temp_dir, "board_gerbers.zip")
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertIn("board-F_Cu.gtl", names)
            self.assertNotIn("board-job.gbrjob", names)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_ibom_bom_field_mapping(self):
        """InteractiveHtmlBom CLI args mirror BOM_EXPORT_DEFAULTS."""
        args = kiforge.build_ibom_cli_args(
            {"dark_mode": True},
            "/tmp/out",
            extra_data_file="/tmp/board.kicad_pcb",
        )
        self.assertIn("--show-fields", args)
        self.assertIn("References,Value,Footprint,Description,Quantity,DNP,ID,MPN", args)
        self.assertIn("--group-fields", args)
        self.assertIn("Value,ID,Footprint,DNP", args)
        self.assertIn("--extra-fields", args)
        self.assertIn("ID,MPN", args)
        self.assertIn("--extra-data-file", args)
        self.assertIn("/tmp/board.kicad_pcb", args)

    def test_export_params_save_and_load_round_trip(self):
        """Placement/STEP options persist in project settings like other exports."""
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            settings = kiforge.DEFAULT_SETTINGS.copy()
            settings["exports"] = kiforge.DEFAULT_EXPORT_SETTINGS.copy()
            settings["export_params"] = {
                "pos_side": "front",
                "pos_smd_only": False,
                "pos_exclude_dnp": False,
            }
            kiforge.save_settings(settings, project_dir=temp_dir, scope="project")
            loaded = kiforge.load_merged_settings(temp_dir)
            self.assertEqual(loaded["export_params"]["pos_side"], "front")
            self.assertFalse(loaded["export_params"]["pos_smd_only"])
            self.assertFalse(loaded["export_params"]["pos_exclude_dnp"])
        finally:
            shutil.rmtree(temp_dir)

    def test_cd_workflow_includes_export_params(self):
        """Advanced placement/STEP/render options are written into generated CD workflows."""
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            options = kiforge.apply_export_params_to_options({
                "export_bom": True,
                "sync_title_block_rev": False,
                "export_params": {
                    "pos_side": "front",
                    "pos_smd_only": False,
                    "pos_exclude_dnp": False,
                    "step_subst_models": False,
                },
            })
            _, success = kiforge.generate_cd_files(temp_dir, "kiforge_out", options)
            self.assertTrue(success)
            workflow_path = os.path.join(temp_dir, ".github", "workflows", "release.yml")
            with open(workflow_path, "r", encoding="utf-8") as f:
                content = f.read()
            for spec in kiforge.EXPORT_PARAM_SPECS:
                placeholder = spec["cd_placeholder"]
                expected = kiforge.build_cd_substitutions("kiforge_out", options)[placeholder]
                self.assertIn(f"{spec['action_input']}: '{expected}'", content)
            self.assertIn("sync_title_block_rev: 'false'", content)
        finally:
            shutil.rmtree(temp_dir)

    def test_cli_export_param_flags(self):
        """CLI exposes every export_params key declared in EXPORT_PARAM_SPECS."""
        args = kiforge.parse_cli_args([
            "--pos-side", "back",
            "--no-pos-smd-only",
            "--no-step-subst-models",
        ])
        options = kiforge.apply_export_params_to_options(kiforge.build_cli_options(args))
        self.assertEqual(options["pos_side"], "back")
        self.assertFalse(options["pos_smd_only"])
        self.assertFalse(options["step_subst_models"])

    def test_cli_top_bottom_aliases(self):
        """--top and --bottom match the placement side conventions from shell scripts."""
        top = kiforge.apply_export_params_to_options(
            kiforge.build_cli_options(kiforge.parse_cli_args(["--top"]))
        )
        bottom = kiforge.apply_export_params_to_options(
            kiforge.build_cli_options(kiforge.parse_cli_args(["--bottom"]))
        )
        self.assertEqual(top["pos_side"], "front")
        self.assertEqual(bottom["pos_side"], "back")

    def test_action_yml_declares_export_param_inputs(self):
        """GitHub Action inputs stay aligned with EXPORT_PARAM_SPECS."""
        action_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "action.yml")
        with open(action_path, "r", encoding="utf-8") as f:
            action_yaml = f.read()
        for spec in kiforge.EXPORT_PARAM_SPECS:
            self.assertIn(f"{spec['action_input']}:", action_yaml)
        for spec in kiforge.RUNTIME_OPTION_SPECS:
            self.assertIn(f"{spec['action_input']}:", action_yaml)

    def test_placement_export_uses_export_params(self):
        """Placement CSV export passes side/SMD/DNP flags to kicad-cli."""
        from unittest.mock import patch

        task = kiforge.PlacementExportTask()
        context = kiforge.ExportContext(
            ".",
            "out",
            kiforge.apply_export_params_to_options(
                {"pos_side": "front", "pos_smd_only": True, "pos_exclude_dnp": False}
            ),
        )
        context.pcb_file = "board.kicad_pcb"
        context.kicad_cli = "kicad-cli"
        context.output_dir = "out"
        captured = {}

        def fake_run(cmd, ctx, **kwargs):
            captured["cmd"] = cmd
            return True

        with patch.object(task, "_run_subprocess", side_effect=fake_run):
            self.assertTrue(task.run(context))
        self.assertIn("--smd-only", captured["cmd"])
        self.assertIn("--side", captured["cmd"])
        self.assertIn("front", captured["cmd"])
        self.assertNotIn("--exclude-dnp", captured["cmd"])

    def test_jlc_format_tasks_respect_format_jlc_flag(self):
        """JLC copies are produced only when format_jlc is enabled."""
        import tempfile
        import csv

        temp_dir = tempfile.mkdtemp()
        out_dir = os.path.join(temp_dir, "out")
        os.makedirs(out_dir)
        try:
            raw_bom = os.path.join(out_dir, "raw_bom.csv")
            with open(raw_bom, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["Reference", "Value", "Footprint", "DNP"])
                writer.writeheader()
                writer.writerow({"Reference": "R1", "Value": "10k", "Footprint": "R_0603", "DNP": ""})

            raw_pos = os.path.join(out_dir, "raw_pos.csv")
            with open(raw_pos, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"]
                )
                writer.writeheader()
                writer.writerow({
                    "Ref": "R1", "Val": "10k", "Package": "R_0603",
                    "PosX": "1", "PosY": "2", "Rot": "0", "Side": "top",
                })

            bom_task = kiforge.BomOutputTask()
            pos_task = kiforge.PosOutputTask()
            jlc_task = kiforge.JlcFormatTask()
            context = kiforge.ExportContext(".", "out", {"format_jlc": False, "export_bom": True, "export_pos": True})
            context.pcb_file = "dummy.kicad_pcb"
            context.sch_file = "dummy.kicad_sch"
            context.pcb_name = "board_v1.0"
            context.project_dir = temp_dir
            context.output_dir = out_dir
            context.logger = kiforge.logger

            self.assertTrue(bom_task.run(context))
            self.assertTrue(pos_task.run(context))
            self.assertFalse(jlc_task.is_applicable(context))

            versioned_bom = os.path.join(out_dir, "board_v1.0_bom.csv")
            versioned_pos = os.path.join(out_dir, "board_v1.0_pos.csv")
            self.assertTrue(os.path.isfile(versioned_bom))
            self.assertTrue(os.path.isfile(versioned_pos))
            self.assertFalse(os.path.isfile(os.path.join(out_dir, "board_v1.0_bom_jlc.csv")))
            self.assertFalse(os.path.isfile(os.path.join(out_dir, "board_v1.0_cpl_jlc.csv")))
        finally:
            shutil.rmtree(temp_dir)

    def test_bom_output_task_produces_kicad_csv(self):
        """Verify versioned KiCad BOM is kept without JLC post-processing in BomOutputTask."""
        import tempfile
        import csv

        temp_dir = tempfile.mkdtemp()
        out_dir = os.path.join(temp_dir, "out")
        os.makedirs(out_dir)
        try:
            raw_bom = os.path.join(out_dir, "raw_bom.csv")
            with open(raw_bom, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["Reference", "Value", "Footprint", "DNP"])
                writer.writeheader()
                writer.writerow({"Reference": "R1", "Value": "10k", "Footprint": "R_0603", "DNP": ""})

            context = kiforge.ExportContext(".", "out", {"format_jlc": True, "export_bom": True})
            context.sch_file = "dummy.kicad_sch"
            context.pcb_name = "sample_v1.0"
            context.output_dir = out_dir
            context.logger = kiforge.logger

            self.assertTrue(kiforge.BomOutputTask().run(context))
            self.assertTrue(os.path.isfile(os.path.join(out_dir, "sample_v1.0_bom.csv")))
            self.assertFalse(os.path.isfile(os.path.join(out_dir, "sample_v1.0_bom_jlc.csv")))
            self.assertFalse(os.path.isfile(raw_bom))
        finally:
            shutil.rmtree(temp_dir)

    def test_format_jlc_exports_from_kicad_csvs(self):
        """JLC copies follow JLCPCB KiCad Method 1 column layout."""
        import tempfile
        import csv

        temp_dir = tempfile.mkdtemp()
        out_dir = os.path.join(temp_dir, "out")
        os.makedirs(out_dir)
        try:
            bom_path = os.path.join(out_dir, "board_v1.0_bom.csv")
            pos_path = os.path.join(out_dir, "board_v1.0_pos.csv")
            with open(bom_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=["Reference", "Value", "Footprint", "${DNP}", "${QUANTITY}", "ID"],
                )
                writer.writeheader()
                writer.writerow({
                    "Reference": "R1",
                    "Value": "10k",
                    "Footprint": "R_0603",
                    "${DNP}": "",
                    "${QUANTITY}": "1",
                    "ID": "C125111",
                })
                writer.writerow({
                    "Reference": "C1",
                    "Value": "100n",
                    "Footprint": "C_0603",
                    "${DNP}": "",
                    "${QUANTITY}": "1",
                    "ID": "custom-cap",
                })
            with open(pos_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(
                    f, fieldnames=["Ref", "Val", "Package", "PosX", "PosY", "Rot", "Side"]
                )
                writer.writeheader()
                writer.writerow({
                    "Ref": "R1", "Val": "10k", "Package": "R_0603",
                    "PosX": "12.5", "PosY": "8.0", "Rot": "90", "Side": "top",
                })

            context = kiforge.ExportContext(".", "out", {"export_bom": True, "export_pos": True})
            context.pcb_name = "board_v1.0"
            context.output_dir = out_dir
            context.logger = kiforge.logger

            self.assertTrue(kiforge.format_jlc_exports(context))
            jlc_bom = os.path.join(out_dir, "board_v1.0_bom_jlc.csv")
            jlc_cpl = os.path.join(out_dir, "board_v1.0_cpl_jlc.csv")
            self.assertTrue(os.path.isfile(jlc_bom))
            self.assertTrue(os.path.isfile(jlc_cpl))

            with open(jlc_bom, encoding="utf-8-sig", newline="") as f:
                bom_reader = csv.DictReader(f)
                self.assertEqual(bom_reader.fieldnames, list(kiforge.JLC_BOM_COLUMNS))
                row = next(bom_reader)
                self.assertEqual(row["Comment"], "10k")
                self.assertEqual(row["Designator"], "R1")
                self.assertEqual(row["Footprint"], "R_0603")
                self.assertEqual(row[kiforge.JLC_BOM_PART_COLUMN], "C125111")
                self.assertEqual(row["Quantity"], "1")
                row2 = next(bom_reader)
                self.assertEqual(row2["Designator"], "C1")
                self.assertEqual(row2[kiforge.JLC_BOM_PART_COLUMN], "")

            with open(jlc_cpl, encoding="utf-8-sig", newline="") as f:
                cpl_reader = csv.DictReader(f)
                self.assertEqual(cpl_reader.fieldnames, list(kiforge.JLC_CPL_COLUMNS))
                row = next(cpl_reader)
                self.assertEqual(row["Designator"], "R1")
                self.assertEqual(row["Mid X"], "12.5")
                self.assertEqual(row["Mid Y"], "8.0")
                self.assertEqual(row["Rotation"], "90")
                self.assertEqual(row["Layer"], "Top")
        finally:
            shutil.rmtree(temp_dir)

    def test_resolve_git_latest_tag(self):
        """Verify latest git tag is resolved from a repository directory."""
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            git = shutil.which("git")
            if not git:
                self.skipTest("git not available")
            subprocess.run([git, "init"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "config", "user.email", "kiforge@test.local"], cwd=temp_dir, check=True)
            subprocess.run([git, "config", "user.name", "KiForge Test"], cwd=temp_dir, check=True)
            marker = os.path.join(temp_dir, "marker.txt")
            with open(marker, "w", encoding="utf-8") as f:
                f.write("test\n")
            subprocess.run([git, "add", "marker.txt"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "commit", "-m", "init"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "tag", "v2.5.0"], cwd=temp_dir, check=True, capture_output=True)
            self.assertEqual(kiforge.resolve_git_latest_tag(temp_dir), "v2.5.0")
        finally:
            _rmtree_force(temp_dir)

    def test_version_from_git_tag(self):
        """Verify local export uses the latest git tag when no explicit version is set."""
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            git = shutil.which("git")
            if not git:
                self.skipTest("git not available")
            pcb_path = os.path.join(temp_dir, "myboard.kicad_pcb")
            with open(pcb_path, "w", encoding="utf-8") as f:
                f.write("(kicad_pcb (version 20240108)\n")
            subprocess.run([git, "init"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "config", "user.email", "kiforge@test.local"], cwd=temp_dir, check=True)
            subprocess.run([git, "config", "user.name", "KiForge Test"], cwd=temp_dir, check=True)
            subprocess.run([git, "add", "myboard.kicad_pcb"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "commit", "-m", "board"], cwd=temp_dir, check=True, capture_output=True)
            subprocess.run([git, "tag", "v3.1.4"], cwd=temp_dir, check=True, capture_output=True)

            context = kiforge.ExportContext(temp_dir, "out", {})
            original_setup_logger = kiforge.setup_logger
            kiforge.setup_logger = lambda dir: None
            try:
                with _without_github_actions_tag_env():
                    self.assertTrue(context.resolve())
            finally:
                kiforge.setup_logger = original_setup_logger
            self.assertEqual(context.pcb_name, "myboard_v3.1.4")
        finally:
            _rmtree_force(temp_dir)

    def test_normalize_version_suffix(self):
        """Verify version strings are normalized for filenames."""
        self.assertEqual(kiforge.normalize_version_suffix("1.2.3"), "v1.2.3")
        self.assertEqual(kiforge.normalize_version_suffix("v1.2.3"), "v1.2.3")
        self.assertEqual(kiforge.normalize_version_suffix("refs/tags/v9.0"), "v9.0")
        self.assertEqual(kiforge.normalize_version_suffix(""), "v0.1.0")

    def test_apply_version_suffix(self):
        """Verify version suffix is applied once and never omitted."""
        self.assertEqual(kiforge.apply_version_suffix("board", "1.0.0"), "board_v1.0.0")
        self.assertEqual(kiforge.apply_version_suffix("board_v1.0.0", "2.0.0"), "board_v1.0.0")

    def test_sanitize_filename_component(self):
        """Untrusted version/name input must not enable path traversal or unsafe files."""
        self.assertEqual(kiforge.sanitize_filename_component("v1.2.3"), "v1.2.3")
        # Directory separators and traversal collapse to the final safe component.
        self.assertEqual(kiforge.sanitize_filename_component("../../etc/passwd"), "passwd")
        self.assertEqual(kiforge.sanitize_filename_component("a\\b\\c"), "c")
        # Shell/HTML metacharacters are replaced, not preserved.
        self.assertNotIn("/", kiforge.sanitize_filename_component("a/b"))
        self.assertNotIn(";", kiforge.sanitize_filename_component("v1;rm -rf"))
        self.assertNotIn("<", kiforge.sanitize_filename_component("<script>"))
        # Empty/degenerate input falls back safely.
        self.assertEqual(kiforge.sanitize_filename_component("", fallback="x"), "x")
        self.assertEqual(kiforge.sanitize_filename_component("...", fallback="x"), "x")

    def test_normalize_version_suffix_rejects_traversal(self):
        """A malicious git tag cannot inject path separators into output filenames."""
        result = kiforge.normalize_version_suffix("v1.0/../../evil")
        self.assertNotIn("/", result)
        self.assertNotIn("..", result)
        # A tag with shell metacharacters is reduced to a safe filename token.
        self.assertNotIn(";", kiforge.normalize_version_suffix("1.0;whoami"))

    def test_build_ibom_cli_args(self):
        """Verify iBOM CLI flags are built from saved settings and BOM defaults."""
        args = kiforge.build_ibom_cli_args(
            {"dark_mode": True, "include_tracks": False, "include_netlist": True},
            "/tmp/out",
        )
        self.assertIn("--no-browser", args)
        self.assertIn("--dark-mode", args)
        self.assertIn("--include-nets", args)
        self.assertIn("--show-fields", args)
        self.assertIn("--group-fields", args)
        self.assertNotIn("--include-netlist", args)
        self.assertNotIn("--include-tracks", args)
        self.assertEqual(args[-2:], ["--dest-dir", "/tmp/out"])

    def test_build_ibom_cli_args_always_suppresses_browser(self):
        """Legacy no_browser=false in saved settings must not open a browser during export."""
        args = kiforge.build_ibom_cli_args({"no_browser": False}, "/tmp/out")
        self.assertEqual(args.count("--no-browser"), 1)

    def test_cleanup_partial_ibom_output(self):
        """Cancelled iBOM runs should remove default and versioned HTML outputs."""
        with tempfile.TemporaryDirectory() as tmp:
            default_path = os.path.join(tmp, "ibom.html")
            versioned_path = os.path.join(tmp, "board_v1_ibom.html")
            open(default_path, "w", encoding="utf-8").close()
            open(versioned_path, "w", encoding="utf-8").close()
            kiforge.cleanup_partial_ibom_output(tmp, "board_v1")
            self.assertFalse(os.path.exists(default_path))
            self.assertFalse(os.path.exists(versioned_path))

    def test_build_ibom_subprocess_command(self):
        """iBOM must run as a module so it does not register a pcbnew ActionPlugin."""
        cmd = kiforge.build_ibom_subprocess_command("/usr/bin/python3")
        self.assertEqual(
            cmd,
            ["/usr/bin/python3", "-m", "InteractiveHtmlBom.generate_interactive_bom"],
        )
        env = kiforge.ensure_ibom_subprocess_env({})
        self.assertEqual(env["INTERACTIVE_HTML_BOM_NO_DISPLAY"], "1")
        self.assertEqual(env["INTERACTIVE_HTML_BOM_CLI_MODE"], "1")

    def test_ibom_env_not_set_on_kiforge_import(self):
        """Importing kiforge must not set iBOM CLI env vars (breaks InteractiveHtmlBom toolbar)."""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = os.environ.copy()
        env.pop("INTERACTIVE_HTML_BOM_CLI_MODE", None)
        env.pop("INTERACTIVE_HTML_BOM_NO_DISPLAY", None)
        code = (
            f"import sys; sys.path.insert(0, {repo_root!r}); "
            "import kiforge; import os; "
            "raise SystemExit(1 if os.environ.get('INTERACTIVE_HTML_BOM_CLI_MODE') "
            "or os.environ.get('INTERACTIVE_HTML_BOM_NO_DISPLAY') else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_format_ibom_failure_message_outline(self):
        """Missing Edge.Cuts outline should produce a clear warning."""
        msg = kiforge.format_ibom_failure_message(
            stderr="2026-06-30 ERROR Please draw pcb outline on the edges layer before generating BOM.\n"
                   "2026-06-30 ERROR Parsing failed.\n"
        )
        self.assertIn("Edge.Cuts", msg)
        self.assertIn("skipped", msg.lower())
        self.assertNotIn("python.exe", msg)

    def test_format_task_failure_message(self):
        """Generic export failures should stay short and omit raw commands."""
        msg = kiforge.format_task_failure_message(
            "Exporting Gerber Layers",
            stderr="ERROR: Failed to export gerbers\n",
        )
        self.assertIn("Gerber", msg)
        self.assertNotIn("kicad-cli", msg.lower())

    def test_export_runner_continues_after_task_failure(self):
        """One failed step must not abort the whole export pipeline."""
        context = kiforge.ExportContext(".", "kiforge_out", {
            "export_gerbers": True,
            "export_drills": False,
            "export_pos": False,
            "export_bom": False,
            "export_ibom": False,
            "export_3d": False,
            "export_svg": False,
            "export_step": False,
            "export_sch_pdf": False,
        })
        context.pcb_file = "board.kicad_pcb"
        context.pcb_name = "board"
        context.project_dir = "."
        context.output_dir = "kiforge_out"
        context.temp_gerber_dir = os.path.join("kiforge_out", "temp_gerbers")
        context.kicad_cli = "kicad-cli"
        context.kicad_python = "python"
        os.makedirs(context.output_dir, exist_ok=True)

        runner = kiforge.ExportRunner(context)
        original_run = runner.tasks[0].run

        def fail_gerbers(ctx):
            ctx.add_warning("Exporting Gerber Layers failed: simulated")
            return False

        runner.tasks[0].run = fail_gerbers
        try:
            result = runner.execute()
        finally:
            runner.tasks[0].run = original_run

        self.assertTrue(result)
        self.assertIn("Exporting Gerber Layers failed", context.warnings[0])

    def test_merged_settings_include_ibom(self):
        """Verify iBOM defaults merge from global/project JSON."""
        import tempfile

        temp_dir = tempfile.mkdtemp()
        global_path = kiforge.get_global_settings_path()
        backup = None
        if os.path.isfile(global_path):
            with open(global_path, "r", encoding="utf-8") as f:
                backup = f.read()
        try:
            kiforge.save_settings({"ibom": {"dark_mode": True}}, scope="global")
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertTrue(merged["ibom"]["dark_mode"])
            project_path = kiforge.get_project_settings_path(temp_dir)
            with open(project_path, "w", encoding="utf-8") as f:
                import json
                json.dump({"ibom": {"dark_mode": False, "checkboxes": True}}, f)
            merged = kiforge.load_merged_settings(temp_dir)
            self.assertFalse(merged["ibom"]["dark_mode"])
            self.assertTrue(merged["ibom"]["checkboxes"])
        finally:
            shutil.rmtree(temp_dir)
            if backup is not None:
                os.makedirs(os.path.dirname(global_path), exist_ok=True)
                with open(global_path, "w", encoding="utf-8") as f:
                    f.write(backup)
            elif os.path.isfile(global_path):
                os.remove(global_path)

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
            
            # Case 2: No explicit version or git tag → default v0.1.0 (title block ignored)
            options = {}
            context = kiforge.ExportContext(temp_dir, "out", options)
            original_setup_logger = kiforge.setup_logger
            kiforge.setup_logger = lambda dir: None
            try:
                with _without_github_actions_tag_env():
                    self.assertTrue(context.resolve())
            finally:
                kiforge.setup_logger = original_setup_logger
            self.assertEqual(context.pcb_name, "myboard_v0.1.0")

            # Case 3: Default to v0.1.0 when no rev, env, or option is available
            plain_dir = tempfile.mkdtemp()
            try:
                plain_pcb = os.path.join(plain_dir, "plain.kicad_pcb")
                with open(plain_pcb, "w", encoding="utf-8") as f:
                    f.write("(kicad_pcb (version 20240108)\n")
                context = kiforge.ExportContext(plain_dir, "out", {})
                kiforge.setup_logger = lambda dir: None
                try:
                    with _without_github_actions_tag_env():
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
        """Partial STEP output should count as success with a warning."""
        task = kiforge.Step3dExportTask()

        options = {"export_step": True}
        context = kiforge.ExportContext(".", "kiforge_out", options)
        context.pcb_file = "dummy.kicad_pcb"
        context.pcb_name = "dummy"
        context.project_dir = "."
        context.output_dir = "kiforge_out"
        context.temp_gerber_dir = "kiforge_out/temp_gerbers"
        os.makedirs(context.output_dir, exist_ok=True)
        output_step = os.path.join(context.output_dir, "dummy.step")

        with open(output_step, "wb") as step_file:
            step_file.write(b"partial-step")

        task._run_subprocess = lambda cmd, ctx: (
            ctx.add_warning("Exporting STEP 3D Model failed: Cannot use VRML models"),
            False,
        )[1]

        self.assertTrue(task.run(context))
        self.assertTrue(any("STEP" in warning for warning in context.warnings))

    def test_tab_icon_svg_cache_round_trip(self):
        """Tab icons are cached as SVG beside global KiForge settings."""
        from unittest.mock import patch

        sample_svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            b'<path d="M0 0h24v24H0z"/></svg>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(kiforge, "tab_icon_cache_dir", return_value=tmp):
                kiforge.write_cached_tab_icon_svg("export", sample_svg)
                loaded = kiforge.read_cached_tab_icon_svg("export")
                self.assertEqual(loaded, sample_svg)

    def test_prepare_tab_icon_svg_adds_light_fill(self):
        """CDN SVGs without fill are tinted for dark tab backgrounds."""
        raw = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            b'<path d="M0 0h24v24H0z"/></svg>'
        )
        prepared = kiforge.prepare_tab_icon_svg(raw)
        self.assertIn(b'fill="#e4e4e7"', prepared)
        with_path_fill = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            b'<path fill="#000000" d="M0 0h24v24H0z"/></svg>'
        )
        self.assertIn(b'fill="#e4e4e7"', kiforge.prepare_tab_icon_svg(with_path_fill))
        self.assertNotIn(b'fill="#000000"', kiforge.prepare_tab_icon_svg(with_path_fill))

    def test_tab_icon_cdn_download_cached(self):
        """CDN icon fetch writes SVG into the local cache."""
        from unittest.mock import MagicMock, patch

        sample_svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24">'
            b'<path d="M0 0h24v24H0z"/></svg>'
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(kiforge, "tab_icon_cache_dir", return_value=tmp):
                mock_resp = MagicMock()
                mock_resp.read.return_value = sample_svg
                mock_resp.__enter__.return_value = mock_resp
                mock_resp.__exit__.return_value = False
                with patch.object(
                    kiforge.urllib.request,
                    "urlopen",
                    return_value=mock_resp,
                ):
                    data = kiforge.download_tab_icon_svg("export")
                self.assertEqual(data, sample_svg)
                self.assertEqual(kiforge.read_cached_tab_icon_svg("export"), sample_svg)

    def test_destroy_progress_dialog_tolerates_none(self):
        """Progress dialog teardown must not raise when no dialog exists."""
        try:
            from plugins import kiforge_studio
        except ModuleNotFoundError as exc:
            if exc.name == "wx":
                self.skipTest("wxPython not installed")
            raise
        kiforge_studio._destroy_progress_dialog(None)


if __name__ == '__main__':
    unittest.main()
