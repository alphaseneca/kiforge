# KiForge Development Guide

## Repository layout

```
kiforge/
├── kiforge.py                 # Core exporter (single source of truth)
├── templates/                 # Editable templates ONLY — do not duplicate elsewhere
│   ├── kiforge.gitignore      # Merged into downstream project .gitignore
│   ├── github-release.yml     # GitHub Actions CD workflow template
│   └── gitea-release.yml      # Gitea Actions CD workflow template
├── plugins/
│   ├── kiforge_studio.py      # KiCad GUI
│   └── kiforge.py             # Auto-copied from root when packaging (gitignored)
├── tests/
│   ├── sample_project/        # Minimal KiCad 10 project
│   ├── test_cli.py
│   └── test_studio.py
└── package_plugin.py          # Builds PCM zip; templates zip to plugins/templates/
```

**Templates:** Edit files under `templates/` only. `package_plugin.py` and the Docker image copy that folder next to the installed module at build time. The `plugins/templates/` path exists only inside the released plugin zip — it is not tracked in git.

## Configuration files

| Scope | Path | Contents |
|---|---|---|
| Project | `<project>/.kiforge.json` | Export toggles, output dir, rotation offsets, `ibom` options |
| Global | `%APPDATA%/kiforge/settings.json` (Windows) | Same keys; loaded first, project overrides |

Load order: built-in defaults → global → project → runtime dialog/CLI flags.

## Running unit tests

```bash
python -m unittest tests/test_cli.py -v
```

GUI tests (wx required):

```powershell
$env:KIFORGE_RUN_GUI_TESTS='1'
python -m unittest tests/test_studio.py -v
```

All tests:

```powershell
$env:KIFORGE_RUN_GUI_TESTS='1'
python -m unittest tests/test_cli.py tests/test_studio.py -v
```

## Full export (local KiCad 10)

```bash
python kiforge.py --project-path tests/sample_project --output-dir kiforge
```

Output filenames always include a version suffix (`_vX.Y.Z`):
- Latest git tag in the project (default, when `use_git_tag_version` is true)
- Or `v0.1.0` when no tag is found
- CI uses `GITHUB_REF_NAME` on tag pushes

Verify BOM/POS pairs:

```
sample_v0.1.2_bom.csv      # KiCad raw BOM
sample_v0.1.2_bom_jlc.csv  # JLCPCB formatted
sample_v0.1.2_pos.csv      # KiCad placement
sample_v0.1.2_cpl_jlc.csv  # JLCPCB CPL
```

## CD workflow generation

```bash
python kiforge.py --generate-cd --project-path tests/sample_project --output-dir kiforge
```

Templates live in `templates/github-release.yml` and `templates/gitea-release.yml`.
KiForge substitutes `{{OUTPUT_DIR}}`, export toggles, `{{KIFORGE_ACTION_REF}}`, and
`{{GITHUB_REF_NAME}}` when generating project workflows. `KIFORGE_ACTION_REF` defaults
to `alphaseneca/kiforge@main`; switch it to a release tag (e.g. `@v1.0.0`) in
`kiforge.py` once you publish a stable composite-action release.

## GitHub Actions (this repository)

| Workflow | Purpose |
|---|---|
| `.github/workflows/test-action.yml` | Unit tests + composite-action export on `tests/sample_project` |
| `.github/workflows/release.yml` | Build PCM zip and publish GitHub Release assets on `v*` tags |

Composite action layout:

- `action.yml` — input definitions; delegates to `action/run.sh`
- `action/run.sh` — Docker build/run and workspace ownership restore
- `Dockerfile` — `kicad/kicad:10.0` + KiForge scripts + InteractiveHtmlBom
- `kiforge.sh` — entrypoint inside the container

When you publish a stable composite-action release, change `KIFORGE_ACTION_REF` in
`kiforge.py` from `@main` to the release tag (e.g. `alphaseneca/kiforge@v1.0.0`).

## Plugin packaging

KiForge uses **KiCad PCM schema v2** (KiCad 10+). The official schema is vendored at
`schemas/pcm.v2.schema.json` (upstream:
[pcm.v2.schema.json](https://gitlab.com/kicad/code/kicad/-/raw/master/kicad/pcm/schemas/pcm.v2.schema.json)).

```bash
# Local zip for Install from file in KiCad PCM
python package_plugin.py

# KiCad → Plugin and Content Manager → Install from file… → dist/com.github.alphaseneca.kiforge.zip

# Release build (GitHub PCM URLs + updates metadata.json)
python package_plugin.py --version v0.2.0
# Upload dist/*.zip plus generated packages.json, repository.json, resources.zip
# End users must use releases/latest/download/repository.json — not main-branch PCM files.

# Optional: custom PCM host (only when you have a real static URL for dist/)
python package_plugin.py --repo-base-url https://example.com/kiforge/
```

The packager validates `metadata.json` for official PCM readiness (maintainer, resource links, stable release versions only — dev `0.0.0` is inserted at build time, not committed).

## Official KiCad PCM repository submission

KiForge targets the [KiCad official addons repository](https://dev-docs.kicad.org/en/addons/#_submission_to_the_official_repository). Requirements covered in this repo:

| Requirement | Location |
|---|---|
| PCM v2 manifest | `metadata.json` |
| Zip layout + archive metadata (no `download_*` in zip) | `package_plugin.py` |
| Issue reporting | [GitHub Issues](https://github.com/alphaseneca/kiforge/issues) + `.github/ISSUE_TEMPLATE/` |
| MIT license (GPL-compatible) | `LICENSE` |
| Public release artifacts | `.github/workflows/release.yml` |

### Submit a new release to KiCad

1. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`
2. Confirm the release workflow uploaded the zip and updated `metadata.json` hashes.
3. Fork [kicad/addons/metadata](https://gitlab.com/kicad/addons/metadata) on GitLab.
4. Copy `metadata.json` to `packages/com.github.alphaseneca.kiforge/metadata.json`.
5. Open a merge request. Do **not** MR [kicad/addons/repository](https://gitlab.com/kicad/addons/repository) — it is updated automatically.

Optional: attach `resources/icon.png` in the metadata MR directory if KiCad requests it.

JLCPCB CSV formatting does not call fab APIs — no commercial-service email to KiCad is required.

## Docker (requires Docker Desktop)

```bash
docker compose run --rm export
```

## Adding a new export task

1. Subclass `ExportTask` in `kiforge.py` — implement `is_applicable()` and `run()`.
2. Register in `ExportRunner._initialize_pipeline()`.
3. Add `--[no-]export-<name>` CLI flag in `parse_cli_args()`.
4. Add matching input to `action.yml` and placeholders to CD templates if needed.

See [ARCHITECTURE.md](ARCHITECTURE.md) for pipeline and context details.
