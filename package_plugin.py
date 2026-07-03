#!/usr/bin/env python3
"""
KiCad Plugin Packaging Script for KiForge
=========================================

Builds the PCM (Plugin and Content Manager) zip in ``dist/``. Uses **PCM schema v2**
(KiCad 10+). Release builds also emit ``packages.json``, ``repository.json``, and
``resources.zip`` for GitHub or an explicit custom host.

``metadata.json`` is the committed package manifest (static v2 fields). The
packager validates it and only overwrites build-derived ``download_*`` fields
for release or custom-host builds — see ``schemas/README.md``.

Usage::

    python package_plugin.py                     # local zip (Install from file in PCM)
    python package_plugin.py --version v0.2.0    # release build + GitHub PCM URLs
    python package_plugin.py --repo-base-url https://example.com/kiforge/
    python package_plugin.py clean               # wipe dist/

The repo-root ``kiforge.py`` is copied to ``plugins/kiforge.py`` before zipping.
Templates are read from ``templates/`` and placed at ``plugins/templates/`` inside
the zip (not committed under ``plugins/templates/`` in git).
"""

import os
import sys
import json
import hashlib
import zipfile
import shutil
import copy
from pathlib import Path


PLUGIN_ID = "com.github.alphaseneca.kiforge"
GITHUB_REPO = "alphaseneca/kiforge"
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"
LOCAL_DEV_VERSION = "0.0.0"
PCM_MAX_DESCRIPTION_LENGTH = 150

# KiCad 10+ PCM schema v2 (see schemas/pcm.v2.schema.json and schemas/README.md).
PCM_SCHEMA_PACKAGE = "https://go.kicad.org/pcm/schemas/v2"
PCM_SCHEMA_GITLAB = (
    "https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v2.schema.json"
)
PCM_SCHEMA_REPOSITORY = f"{PCM_SCHEMA_GITLAB}#/definitions/Repository"
PCM_SCHEMA_VERSION = 2
PCM_SCHEMA_FILE = Path(__file__).resolve().parent / "schemas" / "pcm.v2.schema.json"
PACKAGE_MANIFEST_PATH = Path("metadata.json")

# Applied to every PackageVersion row the packager writes (released or local dev).
KICAD_MIN_VERSION = "10.0"
SUPPORTED_PLATFORMS = ("windows", "macos", "linux")

# PCM v2 Package — required top-level keys in metadata.json (see schemas/pcm.v2.schema.json).
_REQUIRED_PACKAGE_FIELDS = (
    "$schema",
    "identifier",
    "type",
    "name",
    "description",
    "description_full",
    "author",
    "maintainer",
    "license",
    "resources",
    "tags",
    "versions",
)

_REQUIRED_RESOURCE_KEYS = ("homepage", "documentation", "issues")

# PCM v2 PackageVersion — required on each committed version row (download_* filled at build).
_REQUIRED_VERSION_FIELDS = ("version", "status", "kicad_version", "platforms")

# Keys that belong in repository metadata only — never in the zip-embedded metadata.json
# (KiCad official PCM submission requirement).
ARCHIVE_FORBIDDEN_VERSION_KEYS = ("download_url", "download_sha256", "download_size")

# Every path uses forward slashes — KiCad PCM expects POSIX-style zip entries.
REQUIRED_ZIP_ENTRIES = (
    "metadata.json",
    "plugins/__init__.py",
    "plugins/kiforge.py",
    "plugins/kiforge_studio.py",
    "plugins/icon.png",
    "plugins/templates/kiforge.gitignore",
    "plugins/templates/github-release.yml",
    "plugins/templates/gitea-release.yml",
)


def _sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_repository_only_version_fields(version_entry: dict) -> dict:
    """Remove download_* fields from a version row for zip-embedded metadata.json."""
    stripped = copy.deepcopy(version_entry)
    for key in ARCHIVE_FORBIDDEN_VERSION_KEYS:
        stripped.pop(key, None)
    return stripped


def _archive_metadata(manifest: dict, version: str | None) -> dict:
    """
    Return zip-embedded metadata: one PackageVersion entry (KiCad PCM requirement).

    Repository metadata (packages.json / official addons MR) keeps download_* fields;
    the archive inside the zip must not — see KiCad addon publishing docs.
    """
    pcm_version = version.lstrip("v") if version else LOCAL_DEV_VERSION
    entry = next(
        (v for v in manifest.get("versions", []) if v.get("version") == pcm_version),
        None,
    )
    if entry is None:
        raise SystemExit(
            f"PCM verification failed: no version entry for archive metadata ({pcm_version!r})"
        )
    archive = copy.deepcopy(manifest)
    archive["versions"] = [_strip_repository_only_version_fields(entry)]
    return archive


def _verify_archive_metadata(zip_path: Path, version: str | None) -> None:
    """KiCad rejects plugin zips whose embedded metadata.json lists multiple versions."""
    pcm_version = version.lstrip("v") if version else LOCAL_DEV_VERSION
    with zipfile.ZipFile(zip_path, "r") as zf:
        meta = json.loads(zf.read("metadata.json"))
    versions = meta.get("versions", [])
    if len(versions) != 1:
        raise SystemExit(
            "Package verification failed: archive metadata must have a single version; "
            f"found {len(versions)}."
        )
    version_row = versions[0]
    if version_row.get("version") != pcm_version:
        raise SystemExit(
            "Package verification failed: archive metadata version "
            f"{version_row.get('version')!r} != built version {pcm_version!r}."
        )
    present = [key for key in ARCHIVE_FORBIDDEN_VERSION_KEYS if key in version_row]
    if present:
        raise SystemExit(
            "Package verification failed: zip-embedded metadata.json must not contain "
            f"repository-only fields: {', '.join(present)}"
        )


def _verify_pcm_download_entry(zip_path: Path, meta: dict, version: str | None) -> None:
    """Ensure packages.json download_sha256 matches the built zip (KiCad PCM requirement)."""
    pcm_version = version.lstrip("v") if version else LOCAL_DEV_VERSION
    entry = next(
        (v for v in meta.get("versions", []) if v.get("version") == pcm_version),
        None,
    )
    if entry is None:
        raise SystemExit(f"PCM verification failed: no version entry for {pcm_version!r}")
    actual = _sha256(zip_path)
    expected = entry.get("download_sha256", "")
    if actual != expected:
        raise SystemExit(
            f"PCM verification failed: zip SHA-256 mismatch for {pcm_version}.\n"
            f"  zip file:     {actual}\n"
            f"  packages.json: {expected}"
        )
    if zip_path.stat().st_size != entry.get("download_size"):
        raise SystemExit(
            f"PCM verification failed: zip size mismatch for {pcm_version} "
            f"({zip_path.stat().st_size} vs {entry.get('download_size')})."
        )


def verify_release_pcm_artifacts(version_tag: str, base_dir: Path | str = ".") -> None:
    """
    Verify release PCM artifacts: zip hash/size vs packages.json and repository.json.

    Used by .github/workflows/release.yml after ``package_plugin.py --version``.
    Raises SystemExit when any check fails.
    """
    base = Path(base_dir)
    version = version_tag.lstrip("v")
    zip_path = base / "dist" / f"com.github.alphaseneca.kiforge-{version_tag}.zip"
    packages_path = base / "packages.json"
    repo_path = base / "repository.json"

    if not zip_path.is_file():
        raise SystemExit(f"Release zip not found: {zip_path}")
    if not packages_path.is_file():
        raise SystemExit(f"packages.json not found: {packages_path}")
    if not repo_path.is_file():
        raise SystemExit(f"repository.json not found: {repo_path}")

    packages = json.loads(packages_path.read_text(encoding="utf-8"))
    entry = next(
        v for v in packages["packages"][0]["versions"] if v["version"] == version
    )
    zip_sha = _sha256(zip_path)
    if zip_sha != entry["download_sha256"]:
        raise SystemExit(
            f"zip SHA-256 mismatch: zip={zip_sha} packages.json={entry['download_sha256']}"
        )
    if zip_path.stat().st_size != entry["download_size"]:
        raise SystemExit(
            f"zip size mismatch: zip={zip_path.stat().st_size} "
            f"packages.json={entry['download_size']}"
        )

    repo = json.loads(repo_path.read_text(encoding="utf-8"))
    packages_sha = _sha256(packages_path)
    if packages_sha != repo["packages"]["sha256"]:
        raise SystemExit(
            f"packages.json SHA-256 mismatch with repository.json: "
            f"actual={packages_sha} repository={repo['packages']['sha256']}"
        )
    print("PCM hash chain verified.")


def _verify_package(zip_path: Path) -> list[str]:
    """
    Validate PCM zip layout. Returns sorted entry names; raises SystemExit on failure.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        backslash_entries = [n for n in names if "\\" in n]
        if backslash_entries:
            raise SystemExit(
                "Package verification failed: zip entries must use forward slashes, found:\n  "
                + "\n  ".join(backslash_entries)
            )
        missing = [entry for entry in REQUIRED_ZIP_ENTRIES if entry not in names]
        if missing:
            raise SystemExit(
                "Package verification failed: missing required entries:\n  "
                + "\n  ".join(missing)
            )
        return sorted(names)


def _extract_install_size(zip_path: Path) -> int:
    """Return sum of uncompressed sizes (bytes) of all files in the zip."""
    total = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            total += info.file_size
    return total


def _normalize_repo_base(url: str) -> str:
    return url.rstrip("/") + "/"


def _download_url(version: str | None, zip_name: str, repo_base_url: str, use_github: bool) -> str:
    if use_github and version:
        return f"https://github.com/{GITHUB_REPO}/releases/download/{version}/{zip_name}"
    return f"{_normalize_repo_base(repo_base_url)}{zip_name}"


def _repository_asset_urls(repo_base_url: str, use_github: bool) -> tuple[str, str]:
    if use_github:
        base = f"https://github.com/{GITHUB_REPO}/releases/latest/download"
        return f"{base}/packages.json", f"{base}/resources.zip"
    base = _normalize_repo_base(repo_base_url).rstrip("/")
    return f"{base}/packages.json", f"{base}/resources.zip"


def _load_package_manifest() -> dict:
    """
    Load and validate the committed PCM v2 package manifest (metadata.json).

    Static package fields (name, tags, author, …) must be authored in the file.
    The packager only overwrites build-derived download_* fields per version.
    """
    if not PACKAGE_MANIFEST_PATH.is_file():
        raise SystemExit(f"{PACKAGE_MANIFEST_PATH} not found — cannot build PCM repository files.")
    with open(PACKAGE_MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    _validate_package_manifest(manifest)
    return manifest


def _validate_package_manifest(manifest: dict) -> None:
    """Fail fast when metadata.json is not a complete PCM v2 Package manifest."""
    if manifest.get("$schema") != PCM_SCHEMA_PACKAGE:
        raise SystemExit(
            f"{PACKAGE_MANIFEST_PATH} must set \"$schema\" to {PCM_SCHEMA_PACKAGE!r} "
            f"(got {manifest.get('$schema')!r})."
        )
    missing = [key for key in _REQUIRED_PACKAGE_FIELDS if key not in manifest]
    if missing:
        raise SystemExit(
            f"{PACKAGE_MANIFEST_PATH} missing required PCM v2 fields: {', '.join(missing)}"
        )
    if not manifest.get("tags"):
        raise SystemExit(f"{PACKAGE_MANIFEST_PATH} must include a non-empty \"tags\" array.")

    description = manifest.get("description", "")
    if len(description) > PCM_MAX_DESCRIPTION_LENGTH:
        raise SystemExit(
            f"{PACKAGE_MANIFEST_PATH} \"description\" must be at most "
            f"{PCM_MAX_DESCRIPTION_LENGTH} characters (got {len(description)})."
        )

    resources = manifest.get("resources", {})
    resource_missing = [key for key in _REQUIRED_RESOURCE_KEYS if not resources.get(key)]
    if resource_missing:
        raise SystemExit(
            f"{PACKAGE_MANIFEST_PATH} resources must include: {', '.join(resource_missing)}"
        )

    for index, version_row in enumerate(manifest.get("versions", [])):
        row_missing = [key for key in _REQUIRED_VERSION_FIELDS if key not in version_row]
        if row_missing:
            raise SystemExit(
                f"{PACKAGE_MANIFEST_PATH} versions[{index}] missing: {', '.join(row_missing)}"
            )
        if version_row.get("version") == LOCAL_DEV_VERSION:
            raise SystemExit(
                f"{PACKAGE_MANIFEST_PATH} must not list dev version {LOCAL_DEV_VERSION!r}; "
                "local builds insert it at pack time only."
            )
        if version_row.get("status") != "stable":
            raise SystemExit(
                f"{PACKAGE_MANIFEST_PATH} versions[{index}] must use status \"stable\" "
                f"for official-ready releases (got {version_row.get('status')!r})."
            )


def _build_version_entry(
    pcm_version: str,
    status: str,
    release_tag: str | None,
    zip_name: str,
    sha256: str,
    download_size: int,
    install_size: int,
    repo_base_url: str,
    use_github: bool,
    *,
    include_download_fields: bool,
) -> dict:
    """
    Build one PCM v2 PackageVersion from measured zip artifacts.

    kicad_version and platforms come from packager constants. download_* fields
    are included only for release or custom-host repository metadata.
    """
    entry = {
        "version": pcm_version,
        "status": status,
        "kicad_version": KICAD_MIN_VERSION,
        "platforms": list(SUPPORTED_PLATFORMS),
        "install_size": install_size,
    }
    if include_download_fields:
        entry.update({
            "download_url": _download_url(release_tag, zip_name, repo_base_url, use_github),
            "download_sha256": sha256,
            "download_size": download_size,
        })
    return entry


def _manifest_with_build(
    manifest: dict,
    version: str | None,
    zip_name: str,
    sha256: str,
    download_size: int,
    install_size: int,
    repo_base_url: str,
    use_github: bool,
    *,
    include_download_fields: bool,
) -> dict:
    """
    Return a manifest copy with the built version row inserted or updated.

    Does not alter static package fields — only the matching versions[] entry.
    """
    if version:
        pcm_version = version.lstrip("v")
        status = "stable"
    else:
        pcm_version = LOCAL_DEV_VERSION
        status = "testing"

    entry = _build_version_entry(
        pcm_version, status, version, zip_name, sha256, download_size, install_size,
        repo_base_url, use_github, include_download_fields=include_download_fields,
    )

    result = copy.deepcopy(manifest)
    versions: list = result.setdefault("versions", [])
    for idx, existing in enumerate(versions):
        if existing.get("version") == pcm_version:
            versions[idx] = entry
            break
    else:
        versions.insert(0, entry)
    return result


def _generate_pcm_repository_files(
    meta: dict,
    output_dir: Path,
    repo_base_url: str,
    use_github: bool,
) -> tuple[Path, Path, Path]:
    """
    Write packages.json, repository.json, and resources.zip for KiCad PCM custom repos.

    All three files are written to output_dir (typically dist/) alongside the plugin zip.
    """
    import time

    output_dir.mkdir(parents=True, exist_ok=True)
    packages_path = output_dir / "packages.json"
    repo_path = output_dir / "repository.json"
    resources_zip_path = output_dir / "resources.zip"

    packages_data = {
        "$schema": PCM_SCHEMA_PACKAGE,
        "packages": [meta],
    }
    with open(packages_path, "w", encoding="utf-8") as f:
        json.dump(packages_data, f, indent=4)
        f.write("\n")

    packages_sha256 = _sha256(packages_path)

    with zipfile.ZipFile(resources_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        icon_path = Path("resources/icon.png")
        if icon_path.exists():
            zipf.write(icon_path, f"{meta['identifier']}/icon.png")
        else:
            print("  Warning: resources/icon.png not found!")

    resources_sha256 = _sha256(resources_zip_path)
    packages_url, resources_url = _repository_asset_urls(repo_base_url, use_github)

    contact = meta.get("maintainer") or meta.get("author", {})
    maintainer_name = contact.get("name", "KiForge Maintainer")
    maintainer_url = GITHUB_REPO_URL
    contact_info = contact.get("contact", {})
    if contact_info:
        maintainer_url = (
            contact_info.get("web")
            or contact_info.get("github")
            or contact_info.get("issues")
            or GITHUB_REPO_URL
        )

    update_timestamp = int(time.time())
    update_time_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    repo_data = {
        "$schema": PCM_SCHEMA_REPOSITORY,
        "schema_version": PCM_SCHEMA_VERSION,
        "maintainer": {
            "name": maintainer_name,
            "contact": {"url": maintainer_url},
        },
        "name": "KiForge Custom PCM Repository",
        "packages": {
            "url": packages_url,
            "sha256": packages_sha256,
            "update_timestamp": update_timestamp,
            "update_time_utc": update_time_utc,
        },
        "resources": {
            "url": resources_url,
            "sha256": resources_sha256,
            "update_timestamp": update_timestamp,
            "update_time_utc": update_time_utc,
        },
    }
    with open(repo_path, "w", encoding="utf-8") as f:
        json.dump(repo_data, f, indent=4)
        f.write("\n")

    return packages_path, repo_path, resources_zip_path


def _print_pcm_install_help(
    zip_path: Path,
    *,
    use_github: bool,
    version: str | None,
    repo_base_url: str | None,
    repo_path: Path | None,
) -> None:
    print("\nInstall in KiCad:")
    print("  Plugin and Content Manager -> Install from file...")
    print(f"  Select: {zip_path.resolve()}")
    if use_github and version:
        print("\nGitHub PCM repository URL (after uploading release assets):")
        print(f"  https://github.com/{GITHUB_REPO}/releases/latest/download/repository.json")
    elif repo_base_url and repo_path is not None:
        base = _normalize_repo_base(repo_base_url).rstrip("/")
        print("\nCustom PCM repository files:")
        print(f"  {repo_path.parent / 'packages.json'}")
        print(f"  {repo_path}")
        print(f"  {repo_path.parent / 'resources.zip'}")
        print(f"\nCustom repository URL: {base}/repository.json")


def package_plugin(version: str = None, repo_base_url: str | None = None):
    """
    Package the plugin for KiCad Plugin Manager.

    Args:
        version: Optional version string like 'v0.2.0'. Uses GitHub release URLs in PCM files.
        repo_base_url: Base URL where dist/ artifacts are hosted. Required with
                       ``--repo-base-url`` for custom PCM repositories; release builds
                       use GitHub URLs when version is set. Unversioned local builds
                       produce only the installable zip.
    """
    if version:
        version = version.strip()
        if version.startswith("refs/tags/"):
            version = version[len("refs/tags/"):]
        if version and version[0].isdigit():
            version = f"v{version}"

    use_github = bool(version) and repo_base_url is None
    emit_pcm_repository = use_github or bool(repo_base_url)
    resolved_repo_base = _normalize_repo_base(repo_base_url) if repo_base_url else ""

    output_dir = Path("dist")
    output_dir.mkdir(exist_ok=True)

    if version:
        zip_name = f"{PLUGIN_ID}-{version}.zip"
    else:
        zip_name = f"{PLUGIN_ID}.zip"
    zip_path = output_dir / zip_name

    print("Copying root kiforge.py to plugins/kiforge.py...")
    shutil.copy2("kiforge.py", "plugins/kiforge.py")

    template_dir = Path("templates")
    if not template_dir.is_dir():
        print(f"  Warning: templates directory not found: {template_dir}")

    print(f"Packaging plugin: {PLUGIN_ID}")
    print(f"Version:          {version or f'{LOCAL_DEV_VERSION} (local zip)'}")
    print(f"Output file:      {zip_path}")

    files_to_include = [
        ("plugins/__init__.py", "plugins/__init__.py"),
        ("plugins/kiforge_studio.py", "plugins/kiforge_studio.py"),
        ("plugins/kiforge.py", "plugins/kiforge.py"),
        ("plugins/icon.png", "plugins/icon.png"),
        ("resources/icon.png", "resources/icon.png"),
    ]
    if template_dir.is_dir():
        for template_src in sorted(template_dir.iterdir()):
            if template_src.is_file():
                arc_path = f"plugins/templates/{template_src.name}"
                files_to_include.append((str(template_src), arc_path))

    staging_meta_path = output_dir / "metadata.json"

    def _write_zip(metadata_file: str) -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(metadata_file, "metadata.json")
            print(f"  Added: {Path(metadata_file).as_posix()} -> metadata.json")
            for local_path, arc_path in files_to_include:
                arc_path = arc_path.replace("\\", "/")
                if os.path.exists(local_path):
                    zipf.write(local_path, arc_path)
                    print(f"  Added: {Path(local_path).as_posix()} -> {arc_path}")
                else:
                    print(f"  Warning: File not found: {local_path}")

    # Pass 1: build with template metadata to measure the archive.
    _write_zip("metadata.json")
    download_size = zip_path.stat().st_size
    install_size = _extract_install_size(zip_path)
    sha256 = _sha256(zip_path)

    base_meta = _load_package_manifest()
    pcm_meta = _manifest_with_build(
        base_meta, version, zip_name, sha256, download_size, install_size,
        resolved_repo_base, use_github, include_download_fields=emit_pcm_repository,
    )
    archive_meta = _archive_metadata(pcm_meta, version)
    with open(staging_meta_path, "w", encoding="utf-8") as f:
        json.dump(archive_meta, f, indent=4)
        f.write("\n")

    # Pass 2: rebuild with PCM metadata embedded (valid semver + install metadata).
    print("\nRebuilding zip with PCM metadata.json...")
    _write_zip(str(staging_meta_path))
    download_size = zip_path.stat().st_size
    install_size = _extract_install_size(zip_path)
    sha256 = _sha256(zip_path)
    pcm_meta = _manifest_with_build(
        base_meta, version, zip_name, sha256, download_size, install_size,
        resolved_repo_base, use_github, include_download_fields=emit_pcm_repository,
    )

    # Pass 3: metadata inside the zip must carry the same hash KiCad will verify.
    archive_meta = _archive_metadata(pcm_meta, version)
    with open(staging_meta_path, "w", encoding="utf-8") as f:
        json.dump(archive_meta, f, indent=4)
        f.write("\n")
    print("\nFinal zip rebuild (metadata hash synced)...")
    _write_zip(str(staging_meta_path))
    download_size = zip_path.stat().st_size
    install_size = _extract_install_size(zip_path)
    sha256 = _sha256(zip_path)
    pcm_meta = _manifest_with_build(
        base_meta, version, zip_name, sha256, download_size, install_size,
        resolved_repo_base, use_github, include_download_fields=emit_pcm_repository,
    )
    if emit_pcm_repository:
        _verify_pcm_download_entry(zip_path, pcm_meta, version)

    print(f"\nPlugin packaged successfully!")
    print(f"Package location:  {zip_path.absolute()}")
    print(f"Download size:     {download_size / 1024:.1f} KB  ({download_size} bytes)")
    print(f"Install size:      {install_size / 1024:.1f} KB  ({install_size} bytes)")
    print(f"SHA-256:           {sha256}")

    print("\nVerifying package contents:")
    for entry in _verify_package(zip_path):
        print(f"  {entry}")
    _verify_archive_metadata(zip_path, version)
    print("  Package verification passed.")

    repo_path = None
    if emit_pcm_repository:
        packages_path, repo_path, resources_path = _generate_pcm_repository_files(
            pcm_meta, output_dir, resolved_repo_base, use_github,
        )
        print(f"\npackages.json SHA-256:   {_sha256(packages_path)}")
        print(f"resources.zip SHA-256:   {_sha256(resources_path)}")
        print(f"repository.json SHA-256: {_sha256(repo_path)}")
    else:
        for stale_name in ("packages.json", "repository.json", "resources.zip"):
            stale_path = output_dir / stale_name
            if stale_path.is_file():
                stale_path.unlink()
    _print_pcm_install_help(
        zip_path,
        use_github=use_github,
        version=version,
        repo_base_url=repo_base_url,
        repo_path=repo_path,
    )

    if version:
        metadata_path = PACKAGE_MANIFEST_PATH
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(pcm_meta, f, indent=4)
            f.write("\n")
        print(f"\nmetadata.json updated for release version {version.lstrip('v')}.")

        root_packages, root_repo, root_resources = _generate_pcm_repository_files(
            pcm_meta, Path("."), resolved_repo_base, use_github=True,
        )
        print(f"Release PCM copies also written to repo root ({root_packages.name}, "
              f"{root_repo.name}, {root_resources.name}) for GitHub upload.")

    return zip_path


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
        version_arg = None
        repo_base_arg = None
        args = sys.argv[1:]
        if "--version" in args:
            idx = args.index("--version")
            if idx + 1 < len(args):
                version_arg = args[idx + 1]
        if "--repo-base-url" in args:
            idx = args.index("--repo-base-url")
            if idx + 1 < len(args):
                repo_base_arg = args[idx + 1]
        package_plugin(version=version_arg, repo_base_url=repo_base_arg)
