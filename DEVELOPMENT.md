# KiForge Development Guide

## Repository layout

```
kiforge/
├── kiforge.py                 # Core exporter (single source of truth)
├── templates/                 # Editable templates ONLY — do not duplicate elsewhere
│   ├── kiforge.gitignore      # Merged into downstream project .gitignore
│   ├── github-release.yml     # GitHub Actions CD workflow template
│   └── gitea-release.yml      # Gitea Actions CD workflow template
├── icons/                     # Studio UI glyphs, bundled (never fetched at runtime)
├── plugins/
│   ├── __init__.py            # Load bootstrap; owns MIN_PYTHON (see below)
│   ├── kiforge_studio.py      # KiCad GUI
│   └── kiforge.py             # Auto-copied from root when packaging (gitignored)
├── tests/
│   ├── sample_project/        # Minimal KiCad 10 project
│   ├── kicad_runtime_stub.py  # Stand-in wx/pcbnew; runnable as the load gate
│   ├── test_cli.py
│   ├── test_python_compat.py  # Interpreter baseline + "never fail silently"
│   └── test_studio.py
├── pyproject.toml             # Interpreter floor + ruff compatibility rules
└── package_plugin.py          # Builds PCM zip; templates zip to plugins/templates/
```

**Templates:** Edit files under `templates/` only. `package_plugin.py` and the Docker image copy that folder next to the installed module at build time. The `plugins/templates/` path exists only inside the released plugin zip — it is not tracked in git.

## Configuration files

| Scope | Path | Contents |
|---|---|---|
| Project | `<project>/.kiforge.json` | `exports`, `export_params`, `rotation_offsets`, output dir |
| Global | `%APPDATA%/kiforge/settings.json` (Windows) | Same keys; project overrides |

Load order: built-in defaults → global → project → runtime dialog/CLI flags.

## Interpreter baseline

KiCad bundles its own Python and **the version differs per platform** — macOS
10.x ships 3.9.13 inside `KiCad.app`, other platforms ship newer 3.x. So the
supported interpreter is a range with a floor, and every shipped module has to
run across all of it. The floor is declared in four places that a test keeps in
agreement:

| Where | What it does |
|---|---|
| `plugins/__init__.py:MIN_PYTHON` | checked at load time, before any KiForge import |
| `pyproject.toml` `[tool.kiforge] min-python` | the human/tooling-readable declaration |
| `pyproject.toml` `[tool.ruff] target-version` | makes ruff enforce it while you type |
| `.github/workflows/test-action.yml` matrix | what CI actually exercises |

`tests/test_python_compat.py` fails if any of them drift apart. To change the
floor, change all four — the test will tell you if you missed one.

The specific trap: `str | None` (PEP 604) is valid *syntax* on 3.9 but is
evaluated at `def` time, so it raises `TypeError: unsupported operand type(s)
for |` on import. Inside KiCad that is invisible — PCM reports the package
installed and no toolbar button appears. Shipped modules therefore carry
`from __future__ import annotations`, which keeps annotations unevaluated.

Check both before pushing:

```bash
python tests/kicad_runtime_stub.py
```

That imports every shipped module under a stand-in KiCad runtime (fake `wx` and
`pcbnew`) and asserts KiForge registers a toolbar button. It needs no
dependencies and takes under a second. CI runs it on Linux, macOS and Windows
across the interpreter range.

The gate runs under whatever interpreter invokes it, so the strongest local
check is KiCad's own bundled Python — the one the plugin will actually run on:

```bash
# macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 tests/kicad_runtime_stub.py
# Windows
"C:\Program Files\KiCad\10.0\bin\python.exe" tests/kicad_runtime_stub.py
# Linux — KiCad uses the system interpreter
python3 tests/kicad_runtime_stub.py
```

**There is nothing to install for any of this.** KiForge imports only the
standard library plus `wx` and `pcbnew`, and those two come from KiCad itself —
they cannot be installed from PyPI. `pcbnew` is not published there at all, and
KiCad compiles its own wxWidgets, so a PyPI `wxPython` would be ABI-mismatched
with the running KiCad rather than a working substitute. `Pillow` is
the dependency required for PDF rendering, which KiForge manages and installs
automatically into KiCad's bundled Python on startup or via CLI bootstrap.
That is why the compatibility problem is solved by supporting the whole
interpreter range rather than manual environment setup.

```bash
pip install ruff==0.16.6 && ruff check .
```

Ruff is configured narrowly — only `FA`, whose FA102 flags PEP 604 annotations
in a module missing the future import. `ruff check --fix .` adds it for you.

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
| `.github/workflows/test-action.yml` | Load gate, compatibility lint, unit tests, composite-action export |
| `.github/workflows/release.yml` | Build PCM zip and publish GitHub Release assets on `v*` tags |

`test-action.yml` runs four jobs in order:

| Job | Where | Purpose |
|---|---|---|
| `import-gate` | Linux, macOS, Windows × Python 3.9 and 3.12 | Imports every shipped module under a stand-in KiCad and asserts the toolbar button registers. Runs first because it needs no dependencies and pinpoints load breakage |
| `lint` | Linux | `ruff check .` — the interpreter-compatibility rules |
| `unit-tests` | Linux, macOS, Windows × Python 3.9 and 3.12 | The suite, on every platform |
| `integration` | Linux | Composite-action export on `tests/sample_project` |

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
