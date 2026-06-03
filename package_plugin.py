#!/usr/bin/env python3
"""
KiCad Plugin Packaging Script for KiForge
Packages the KiForge plugin for KiCad Plugin Manager

Usage:
    python package_plugin.py                     # local dev build (no version)
    python package_plugin.py --version v0.2.0    # versioned release build
    python package_plugin.py clean               # wipe dist/
"""

import os
import sys
import json
import hashlib
import zipfile
import shutil
from pathlib import Path


PLUGIN_ID = "com.github.alphaseneca.kiforge"
GITHUB_REPO = "alphaseneca/kiforge"


def _sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_install_size(zip_path: Path) -> int:
    """Return sum of uncompressed sizes (bytes) of all files in the zip."""
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            total += info.file_size
    return total


def package_plugin(version: str = None):
    """
    Package the plugin for KiCad Plugin Manager.

    Args:
        version: Optional version string like 'v0.2.0'. When given the zip is
                 named  com.github.alphaseneca.kiforge-v0.2.0.zip  and
                 metadata.json is updated with real download_url / sha256 /
                 download_size / install_size values.
    """
    # Normalise version string
    if version:
        version = version.strip()
        if version.startswith("refs/tags/"):
            version = version[len("refs/tags/"):]
        # Ensure it starts with 'v'
        if version and version[0].isdigit():
            version = f"v{version}"

    # Create output directory
    output_dir = Path("dist")
    output_dir.mkdir(exist_ok=True)

    # Choose zip filename (versioned or plain)
    if version:
        zip_name = f"{PLUGIN_ID}-{version}.zip"
    else:
        zip_name = f"{PLUGIN_ID}.zip"
    zip_path = output_dir / zip_name

    # Copy root-level kiforge.py to plugins/kiforge.py (single source of truth)
    print("Copying root kiforge.py to plugins/kiforge.py...")
    shutil.copy2("kiforge.py", "plugins/kiforge.py")

    print(f"Packaging plugin: {PLUGIN_ID}")
    print(f"Version:          {version or '(unversioned)'}")
    print(f"Output file:      {zip_path}")

    # Files to include in the package: (local_path, zip_path)
    files_to_include = [
        ("metadata.json",           "metadata.json"),
        ("plugins/__init__.py",     "plugins/__init__.py"),
        ("plugins/kiforge_studio.py","plugins/kiforge_studio.py"),
        ("plugins/kiforge.py",      "plugins/kiforge.py"),
        ("plugins/icon.png",        "plugins/icon.png"),
        ("resources/icon.png",      "resources/icon.png"),
    ]

    # Build the zip
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for local_path, arc_path in files_to_include:
            if os.path.exists(local_path):
                zipf.write(local_path, arc_path)
                print(f"  Added: {local_path} -> {arc_path}")
            else:
                print(f"  Warning: File not found: {local_path}")

    download_size = zip_path.stat().st_size
    install_size  = _extract_install_size(zip_path)
    sha256        = _sha256(zip_path)

    print(f"\nPlugin packaged successfully!")
    print(f"Package location:  {zip_path.absolute()}")
    print(f"Download size:     {download_size / 1024:.1f} KB  ({download_size} bytes)")
    print(f"Install size:      {install_size  / 1024:.1f} KB  ({install_size} bytes)")
    print(f"SHA-256:           {sha256}")

    # Verify the package structure
    print("\nVerifying package contents:")
    with zipfile.ZipFile(zip_path, "r") as zipf:
        for info in zipf.filelist:
            print(f"  {info.filename}")

    # ------------------------------------------------------------------
    # Update metadata.json with real values when a version is given
    # ------------------------------------------------------------------
    if version:
        _update_metadata(version, zip_name, sha256, download_size, install_size)

    return zip_path


def _update_metadata(version: str, zip_name: str, sha256: str,
                     download_size: int, install_size: int):
    """Patch metadata.json: update or insert the version entry with real values."""
    metadata_path = Path("metadata.json")
    if not metadata_path.exists():
        print("\nWarning: metadata.json not found – skipping metadata update.")
        return

    with open(metadata_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    # Strip leading 'v' for the KiCad PCM version field (it expects plain semver)
    pcm_version = version.lstrip("v")
    download_url = (
        f"https://github.com/{GITHUB_REPO}/releases/download/{version}/{zip_name}"
    )

    versions: list = meta.setdefault("versions", [])

    # Find an existing entry for this version and update it, or prepend a new one
    for entry in versions:
        if entry.get("version") == pcm_version:
            entry["download_url"]  = download_url
            entry["download_sha256"] = sha256
            entry["download_size"] = download_size
            entry["install_size"]  = install_size
            entry["status"]        = "stable"
            break
    else:
        versions.insert(0, {
            "version":       pcm_version,
            "status":        "stable",
            "kicad_version": "10.0",
            "download_url":  download_url,
            "download_sha256": sha256,
            "download_size": download_size,
            "install_size":  install_size,
        })

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=4)
        f.write("\n")

    print(f"\nmetadata.json updated:")
    print(f"  version:        {pcm_version}")
    print(f"  download_url:   {download_url}")
    print(f"  download_sha256:{sha256}")
    print(f"  download_size:  {download_size}")
    print(f"  install_size:   {install_size}")

    # Generate custom PCM repository files packages.json and repository.json
    _generate_pcm_repository_files(meta)


def _generate_pcm_repository_files(meta: dict):
    """Generate packages.json, repository.json, and resources.zip for custom PCM hosting."""
    import time
    packages_path = Path("packages.json")
    repo_path = Path("repository.json")
    resources_zip_path = Path("resources.zip")
    
    # 1. Generate packages.json
    packages_data = {
        "$schema": "https://go.kicad.org/pcm/schemas/v1",
        "packages": [meta]
    }
    
    with open(packages_path, "w", encoding="utf-8") as f:
        json.dump(packages_data, f, indent=4)
        f.write("\n")
    print(f"\npackages.json generated/updated.")
    
    # Compute SHA-256 of packages.json
    packages_sha256 = _sha256(packages_path)
    
    # 2. Generate resources.zip containing the package icon
    print("Generating resources.zip...")
    with zipfile.ZipFile(resources_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        icon_path = Path("resources/icon.png")
        if icon_path.exists():
            # In the resources archive, the icon must be located at <package_identifier>/icon.png
            zipf.write(icon_path, f"{meta['identifier']}/icon.png")
            print(f"  Added icon: {icon_path} -> {meta['identifier']}/icon.png")
        else:
            print("  Warning: resources/icon.png not found!")
            
    # Compute SHA-256 of resources.zip
    resources_sha256 = _sha256(resources_zip_path)
    
    # 3. Generate repository.json
    author_name = "Ukesh Aryal"
    author_url = "https://github.com/alphaseneca/kiforge"
    
    if "author" in meta:
        author_name = meta["author"].get("name", author_name)
        if "contact" in meta["author"]:
            author_url = meta["author"]["contact"].get("web", meta["author"]["contact"].get("github", author_url))
            
    update_timestamp = int(time.time())
    update_time_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    
    repo_data = {
        "$schema": "https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v1.schema.json#/definitions/Repository",
        "maintainer": {
            "name": author_name,
            "contact": {
                "url": author_url
            }
        },
        "name": "KiForge Custom PCM Repository",
        "packages": {
            "url": f"https://github.com/{GITHUB_REPO}/releases/latest/download/packages.json",
            "sha256": packages_sha256,
            "update_timestamp": update_timestamp,
            "update_time_utc": update_time_utc
        },
        "resources": {
            "url": f"https://github.com/{GITHUB_REPO}/releases/latest/download/resources.zip",
            "sha256": resources_sha256,
            "update_timestamp": update_timestamp,
            "update_time_utc": update_time_utc
        }
    }
    
    with open(repo_path, "w", encoding="utf-8") as f:
        json.dump(repo_data, f, indent=4)
        f.write("\n")
    print(f"repository.json generated/updated with packages.json SHA-256: {packages_sha256} and resources.zip SHA-256: {resources_sha256}")


def clean_dist():
    """Clean the dist directory."""
    dist_dir = Path("dist")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
        print("Cleaned dist directory")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean_dist()
    else:
        # Parse optional --version flag
        version_arg = None
        args = sys.argv[1:]
        if "--version" in args:
            idx = args.index("--version")
            if idx + 1 < len(args):
                version_arg = args[idx + 1]
        package_plugin(version=version_arg)
