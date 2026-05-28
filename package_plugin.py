#!/usr/bin/env python3
"""
KiCad Plugin Packaging Script for KiForge
Packages the KiForge plugin for KiCad Plugin Manager
"""

import os
import zipfile
import shutil
from pathlib import Path

def package_plugin():
    """Package the plugin for KiCad Plugin Manager"""
    
    # Plugin identifier from metadata
    plugin_id = "com.github.alphaseneca.kiforge"
    
    # Create output directory
    output_dir = Path("dist")
    output_dir.mkdir(exist_ok=True)
    
    # Output zip file name
    zip_filename = output_dir / f"{plugin_id}.zip"
    
    # Copy root-level kiforge.py to plugins/kiforge.py to populate it for local development/zips
    print("Copying root kiforge.py to plugins/kiforge.py...")
    shutil.copy2("kiforge.py", "plugins/kiforge.py")
    
    print(f"Packaging plugin: {plugin_id}")
    print(f"Output file: {zip_filename}")
    
    # Files to include in the package: (local_path, zip_path)
    files_to_include = [
        ("metadata.json", "metadata.json"),
        ("plugins/__init__.py", "plugins/__init__.py"),
        ("plugins/kiforge_studio.py", "plugins/kiforge_studio.py"),
        ("plugins/kiforge.py", "plugins/kiforge.py"),
        ("plugins/icon.png", "plugins/icon.png"),
        ("resources/icon.png", "resources/icon.png")
    ]
    
    # Create the zip file
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for local_path, zip_path in files_to_include:
            if os.path.exists(local_path):
                # Add file to zip mapping local path to the target zip path
                zipf.write(local_path, zip_path)
                print(f"  Added: {local_path} -> {zip_path}")
            else:
                print(f"  Warning: File not found: {local_path}")
    
    print(f"\nPlugin packaged successfully!")
    print(f"Package location: {zip_filename.absolute()}")
    print(f"Package size: {zip_filename.stat().st_size / 1024:.1f} KB")
    
    # Verify the package structure
    print("\nVerifying package contents:")
    with zipfile.ZipFile(zip_filename, 'r') as zipf:
        for file_info in zipf.filelist:
            print(f"  {file_info.filename}")

def clean_dist():
    """Clean the dist directory"""
    dist_dir = Path("dist")
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
        print("Cleaned dist directory")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean_dist()
    else:
        package_plugin()
