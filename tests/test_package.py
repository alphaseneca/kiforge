"""
Tests for package_plugin.py — PCM zip layout, metadata, and template resolution.

Running tests rebuilds the plugin zip under ``dist/``.
"""
import json
import os
import sys
import unittest
import zipfile
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pathlib import Path

import package_plugin


class TestPackagePlugin(unittest.TestCase):
    def test_package_contains_required_pcm_entries(self):
        """Verify the plugin zip includes every file KiCad PCM needs."""
        zip_path = package_plugin.package_plugin()
        names = package_plugin._verify_package(zip_path)
        for entry in package_plugin.REQUIRED_ZIP_ENTRIES:
            self.assertIn(entry, names)
        package_plugin._verify_archive_metadata(zip_path, None)

    def test_archive_metadata_has_single_version(self):
        """Embedded metadata.json must list exactly one version for Install from file."""
        zip_path = package_plugin.package_plugin()
        with zipfile.ZipFile(zip_path, "r") as zf:
            meta = json.load(zf.open("metadata.json"))
        self.assertEqual(len(meta["versions"]), 1)
        self.assertEqual(meta["versions"][0]["version"], package_plugin.LOCAL_DEV_VERSION)

    def test_local_build_produces_zip_only(self):
        """Unversioned builds emit only the installable zip — no repository metadata."""
        zip_path = package_plugin.package_plugin()
        dist = Path("dist")
        self.assertTrue(zip_path.is_file())
        self.assertFalse((dist / "repository.json").is_file())
        self.assertFalse((dist / "packages.json").is_file())
        self.assertFalse((dist / "resources.zip").is_file())

    def test_custom_host_pcm_artifacts_in_dist(self):
        """Explicit --repo-base-url builds emit PCM v2 repo files with matching SHA256."""
        zip_path = package_plugin.package_plugin(
            repo_base_url="https://example.com/kiforge/",
        )
        dist = Path("dist")
        repo_path = dist / "repository.json"
        packages_path = dist / "packages.json"
        resources_path = dist / "resources.zip"
        self.assertTrue(repo_path.is_file())
        self.assertTrue(packages_path.is_file())
        self.assertTrue(resources_path.is_file())

        with open(packages_path, "r", encoding="utf-8") as f:
            packages = json.load(f)
        self.assertEqual(packages["$schema"], package_plugin.PCM_SCHEMA_PACKAGE)
        dev_versions = [
            v for v in packages["packages"][0]["versions"]
            if v.get("version") == package_plugin.LOCAL_DEV_VERSION
        ]
        self.assertEqual(len(dev_versions), 1)
        dev = dev_versions[0]
        self.assertRegex(dev["version"], r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
        self.assertEqual(dev["download_sha256"], package_plugin._sha256(zip_path))
        self.assertIn(zip_path.name, dev["download_url"])
        self.assertTrue(dev["download_url"].startswith("https://example.com/kiforge/"))
        self.assertEqual(dev["platforms"], list(package_plugin.SUPPORTED_PLATFORMS))

        with open(repo_path, "r", encoding="utf-8") as f:
            repo = json.load(f)
        self.assertEqual(repo["schema_version"], package_plugin.PCM_SCHEMA_VERSION)
        self.assertIn("pcm.v2.schema.json", repo["$schema"])
        self.assertEqual(repo["packages"]["sha256"], package_plugin._sha256(packages_path))
        self.assertEqual(repo["resources"]["sha256"], package_plugin._sha256(resources_path))

    def test_bundled_pcm_v2_schema_file_exists(self):
        """The official PCM v2 schema is vendored for offline reference."""
        schema_path = package_plugin.PCM_SCHEMA_FILE
        self.assertTrue(schema_path.is_file(), f"Missing {schema_path}")
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        self.assertEqual(schema["$id"], package_plugin.PCM_SCHEMA_PACKAGE)
        self.assertIn("Package", schema.get("definitions", {}))

    def test_archive_metadata_uses_pcm_v2_schema(self):
        """Embedded zip metadata.json must declare PCM schema v2."""
        zip_path = package_plugin.package_plugin()
        with zipfile.ZipFile(zip_path, "r") as zf:
            meta = json.load(zf.open("metadata.json"))
        self.assertEqual(meta["$schema"], package_plugin.PCM_SCHEMA_PACKAGE)
        self.assertIn("tags", meta)
        self.assertEqual(meta["versions"][0]["platforms"], list(package_plugin.SUPPORTED_PLATFORMS))

    def test_archive_metadata_omits_download_fields(self):
        """Zip-embedded metadata must not include repository-only download_* keys."""
        zip_path = package_plugin.package_plugin()
        with zipfile.ZipFile(zip_path, "r") as zf:
            version_row = json.load(zf.open("metadata.json"))["versions"][0]
        for key in package_plugin.ARCHIVE_FORBIDDEN_VERSION_KEYS:
            self.assertNotIn(key, version_row, f"archive metadata must omit {key!r}")
        self.assertIn("install_size", version_row)
        self.assertIn("version", version_row)

        package_plugin.package_plugin(repo_base_url="https://example.com/kiforge/")
        with open(Path("dist") / "packages.json", encoding="utf-8") as f:
            repo_version = next(
                v for v in json.load(f)["packages"][0]["versions"]
                if v["version"] == package_plugin.LOCAL_DEV_VERSION
            )
        for key in package_plugin.ARCHIVE_FORBIDDEN_VERSION_KEYS:
            self.assertIn(key, repo_version, f"repository metadata must retain {key!r}")

    def test_package_manifest_validation_requires_tags(self):
        """Build must fail when metadata.json omits required PCM v2 fields."""
        manifest_path = package_plugin.PACKAGE_MANIFEST_PATH
        original = manifest_path.read_text(encoding="utf-8")
        try:
            data = json.loads(original)
            data.pop("tags", None)
            manifest_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                package_plugin._load_package_manifest()
        finally:
            manifest_path.write_text(original, encoding="utf-8")

    def test_package_manifest_official_submission_fields(self):
        """Committed metadata.json includes author and PCM resource links."""
        manifest = package_plugin._load_package_manifest()
        self.assertIn("author", manifest)
        self.assertLessEqual(
            len(manifest["description"]),
            package_plugin.PCM_MAX_DESCRIPTION_LENGTH,
        )
        for key in package_plugin._REQUIRED_RESOURCE_KEYS:
            self.assertIn(key, manifest["resources"])

    def test_committed_manifest_versions_empty(self):
        """Version rows live in release CI output only; git keeps versions: []."""
        manifest = package_plugin._load_package_manifest()
        self.assertEqual(manifest["versions"], [])

    def test_pcm_icon_files_exist(self):
        """PCM zip and resources.zip require committed plugin and listing icons."""
        for rel in ("plugins/icon.png", "resources/icon.png"):
            path = Path(rel)
            self.assertTrue(path.is_file(), f"Missing {rel}")
            self.assertGreater(path.stat().st_size, 0, f"Empty {rel}")

    def test_versioned_zip_pins_action_ref(self):
        """Release zips pin KIFORGE_ACTION_REF to the release tag, not @main."""
        zip_path = package_plugin.package_plugin(version="v9.9.9")
        with zipfile.ZipFile(zip_path, "r") as zf:
            source = zf.read("plugins/kiforge.py").decode("utf-8")
        self.assertIn('KIFORGE_ACTION_REF = "alphaseneca/kiforge@v9.9.9"', source)
        self.assertNotIn("@main", source)

    def test_release_repository_json_uses_tag_urls(self):
        """Tag release repository.json points at that tag's packages.json, not @main."""
        package_plugin.package_plugin(version="v9.9.8")
        with open(Path("dist") / "repository.json", encoding="utf-8") as f:
            repo = json.load(f)
        self.assertIn(
            "https://github.com/alphaseneca/kiforge/releases/download/v9.9.8/packages.json",
            repo["packages"]["url"],
        )

    def test_installed_layout_resolves_templates(self):
        """Simulate PCM install layout and verify template lookup works."""
        zip_path = package_plugin.package_plugin()
        temp_root = tempfile.mkdtemp()
        plugins_dir = os.path.join(temp_root, "plugins")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(temp_root)
            sys.path.insert(0, plugins_dir)
            if "kiforge" in sys.modules:
                del sys.modules["kiforge"]
            import kiforge  # noqa: F401 — installed copy under plugins/

            gitignore = kiforge.get_template_path("kiforge.gitignore")
            github_yml = kiforge.get_template_path("github-release.yml")
            self.assertTrue(gitignore and os.path.isfile(gitignore))
            self.assertTrue(github_yml and os.path.isfile(github_yml))
            self.assertIn("templates", gitignore.replace("\\", "/"))
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            if plugins_dir in sys.path:
                sys.path.remove(plugins_dir)
            sys.modules.pop("kiforge", None)
            root_kiforge = os.path.join(os.path.dirname(__file__), "..", "kiforge.py")
            if "kiforge" not in sys.modules:
                import importlib.util
                spec = importlib.util.spec_from_file_location("kiforge", root_kiforge)
                mod = importlib.util.module_from_spec(spec)
                sys.modules["kiforge"] = mod


if __name__ == "__main__":
    unittest.main()
