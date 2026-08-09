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
| Project | `<project>/.kiforge.json` | `exports`, `export_params`, `rotation_offsets`, output dir |
| Global | `%APPDATA%/kiforge/settings.json` (Windows) | Same keys; project overrides |

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

Verify BOM/POS pairs (raw KiCad + optional JLC copies):

```
sample_v0.1.2_bom.csv      # Reference, Value, Footprint, Description, ${QUANTITY}, ${DNP}, ID, MPN
sample_v0.1.2_pos.csv      # Ref, Val, Package, PosX, PosY, Rot, Side
sample_v0.1.2_bom_jlc.csv  # Comment, Designator, Footprint, LCSC Part #, Quantity
sample_v0.1.2_cpl_jlc.csv  # Designator, Mid X, Mid Y, Rotation, Layer
sample_v0.1.2_gerbers.zip  # JLC manufacturing layers + Dwgs.User + Cmts.User
```

`JlcFormatTask` / `JLCPCBFormatter` produce JLC copies from the raw CSVs when `format_jlc` is enabled. `ID` values matching `^C\d+$` populate `LCSC Part #`.

## CD workflow generation

```bash
python kiforge.py --generate-cd --project-path tests/sample_project --output-dir kiforge
```

Templates live in `templates/github-release.yml` and `templates/gitea-release.yml`.
KiForge substitutes `{{OUTPUT_DIR}}`, export toggles, `{{KIFORGE_ACTION_REF}}`, and
`{{GITHUB_REF_NAME}}` when generating project workflows. End users who install from a
**PCM release zip** get `alphaseneca/kiforge@vX.Y.Z` baked in (see `PCM_SUBMISSION.md`).
The `@main` default in repo-root `kiforge.py` is for **contributors** running
`--generate-cd` from a git clone only — not a PCM install path.

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

Release plugin zips pin `KIFORGE_ACTION_REF` at package time (see `PCM_SUBMISSION.md`).

## Plugin packaging

PCM publishing (releases, GitLab submission, user install URLs): **[PCM_SUBMISSION.md](PCM_SUBMISSION.md)**.

Schema reference: `schemas/README.md` and vendored `schemas/pcm.v2.schema.json`.

```bash
# Local zip (contributors / Install from file smoke test)
python package_plugin.py

# Release build (same as tag CI — artifacts go to GitHub Release, not committed to git)
python package_plugin.py --version vX.Y.Z

# Optional custom PCM host
python package_plugin.py --repo-base-url https://example.com/kiforge/
```

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
