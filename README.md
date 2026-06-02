<div align="center">

# ⚡ KiForge

### Automated manufacturing & documentation exporter for KiCad 10

[![Test Action](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml/badge.svg)](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml)
[![Release](https://github.com/alphaseneca/kiforge/actions/workflows/release.yml/badge.svg)](https://github.com/alphaseneca/kiforge/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![KiCad 10](https://img.shields.io/badge/KiCad-10.0-blue?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iI2ZmZiIgZD0iTTEyIDJDNi40NzcgMiAyIDYuNDc3IDIgMTJzNC40NzcgMTAgMTAgMTAgMTAtNC40NzcgMTAtMTBTMTcuNTIzIDIgMTIgMnoiLz48L3N2Zz4=)](https://www.kicad.org/)

**KiForge** generates all files needed to order and document your PCB — from Gerbers and BOMs to 3D renders and Interactive HTML BOMs — in a single run. Use it as a KiCad GUI plugin, a reusable GitHub Action, or a plain CLI script.

![KiForge Banner](assets/kiforge.png)

</div>

---

## ✨ What it exports

All outputs are versioned automatically — when triggered by a Git tag (e.g. `v1.2.0`), every filename becomes `<boardname>_v1.2.0_<type>.<ext>`.

| Output | Filename | Toggle |
|---|---|---|
| Gerber + Drill archive | `<name>_gerbers.zip` | `export_gerbers` / `export_drills` |
| JLCPCB Bill of Materials | `<name>_bom_jlc.csv` | `export_bom` |
| JLCPCB Component Placement | `<name>_cpl_jlc.csv` | `export_pos` |
| Schematic PDF | `<name>_sch.pdf` | `export_sch_pdf` |
| STEP 3D Model | `<name>.step` | `export_step` |
| 3D Front & Back Renders | `<name>_3d_front/back.png` | `export_3d` |
| Copper Layer SVGs | `<name>_front/back.svg` | `export_svg` |
| Interactive HTML BOM | `<name>_ibom.html` | `export_ibom` |

---

## 🚀 Three ways to use KiForge

### 1 — KiCad Plugin (GUI)

The quickest way. KiForge appears under **Tools › External Plugins** inside the KiCad PCB Editor. No terminal needed.

**Install via KiCad Plugin Manager** — search for `KiForge` — or drop the `plugins/` folder manually:

| Platform | Plugin path |
|---|---|
| Windows | `%APPDATA%\kicad\10.0\scripting\plugins\com.github.alphaseneca.kiforge\` |
| macOS | `~/Library/Application Support/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/` |
| Linux | `~/.local/share/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/` |

**Workflow:**
1. Open your `.kicad_pcb` in KiCad 10.
2. Go to **Tools › External Plugins › KiForge**.
3. Confirm your project directory and toggle the exports you need.
4. Click **Run Export Now** — a live progress dialog tracks each step.
5. All files land in the configured output folder (default: `kiforge/` next to your `.kicad_pro`).

> **Bonus:** The plugin has a **Generate CI Files** button that writes a complete GitHub Actions release workflow and updates your `.gitignore` automatically — no manual YAML editing required.

---

### 2 — GitHub Action (CI/CD)

Add one step to your workflow and every Git tag push automatically generates a GitHub Release with all manufacturing files attached as downloadable assets. **Nothing is ever committed to your repository.**

#### Quickstart

Create `.github/workflows/release.yml` in your KiCad project:

```yaml
name: Manufacturing Release

on:
  push:
    tags:
      - 'v*'   # e.g. v1.0.0, v2.3.1

permissions:
  contents: write

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'

      - name: Create Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: kiforge/*
```

> Tip: Use the **Generate CI Files** button in the KiCad plugin to create this file automatically.

#### Triggering a release

```bash
git tag -a v1.0.0 -m "First stable release"
git push origin v1.0.0
```

GitHub Actions will:
1. Pull the official `kicad/kicad:10.0` Docker image
2. Run the full KiForge export pipeline
3. Version all output filenames with the tag (e.g. `myboard_v1.0.0_gerbers.zip`)
4. Create a GitHub Release and attach every file as a downloadable asset

#### All inputs

Every input is optional. All exports are enabled by default — set to `'false'` to skip.

| Input | Description | Default |
|---|---|---|
| `project_path` | Path to folder containing `.kicad_pro` | `'.'` |
| `output_dir` | Output folder (relative to `project_path`) | `'kiforge'` |
| `export_gerbers` | Gerber layer files (zipped) | `'true'` |
| `export_drills` | Drill files (included in Gerber ZIP) | `'true'` |
| `export_bom` | JLCPCB Bill of Materials CSV | `'true'` |
| `export_pos` | JLCPCB Component Placement CSV | `'true'` |
| `export_sch_pdf` | Schematic PDF | `'true'` |
| `export_step` | STEP 3D model | `'true'` |
| `export_3d` | Front & back 3D renders (PNG) | `'true'` |
| `export_svg` | Front & back copper layer SVGs | `'true'` |
| `export_ibom` | Interactive HTML BOM | `'true'` |

#### Common recipes

<details>
<summary>JLCPCB fabrication only</summary>

Exports only what JLCPCB needs to manufacture and assemble: Gerbers, Drills, BOM, CPL.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          export_3d: 'false'
          export_svg: 'false'
          export_sch_pdf: 'false'
          export_step: 'false'
          export_ibom: 'false'
```
</details>

<details>
<summary>Documentation only</summary>

Schematic PDF, 3D renders, and SVGs — no fabrication data.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          export_gerbers: 'false'
          export_drills: 'false'
          export_bom: 'false'
          export_pos: 'false'
          export_step: 'false'
          export_ibom: 'false'
```
</details>

<details>
<summary>Project in a subdirectory</summary>

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: 'hardware/my-board'
          output_dir: 'hardware/my-board/kiforge'
```
</details>

---

### 3 — CLI / Docker

Run KiForge directly from the command line or inside your own Docker container:

```bash
# Minimal — runs against the current directory
python kiforge.py

# Custom project and output paths
python kiforge.py --project-path ./my-board --output-dir ./out

# With explicit version tag (appended to all output filenames)
python kiforge.py --project-path . --version-tag v1.2.0

# Selective export
python kiforge.py --no-export-3d --no-export-svg

# Test against the official KiCad Docker image locally
docker compose run --rm export
```

**CLI flags:**

| Flag | Description |
|---|---|
| `--project-path PATH` | Path to KiCad project directory |
| `--output-dir DIR` | Output directory name or path |
| `--version-tag TAG` | Version string appended to all output filenames |
| `--no-export-3d` | Skip 3D renders |
| `--no-export-svg` | Skip SVG export |
| `--no-export-bom` | Skip BOM export |
| `--no-export-sch-pdf` | Skip schematic PDF |
| `--no-export-pos` | Skip CPL/placement file |
| `--no-export-step` | Skip STEP export |
| `--no-export-gerbers` | Skip Gerber export |
| `--no-export-drills` | Skip drill file export |
| `--no-export-ibom` | Skip Interactive HTML BOM |
| `--generate-ci` | Generate GitHub Actions workflow + update `.gitignore` instead of exporting |

---

## 🏷️ Automatic Version Tagging

KiForge appends the version to **all** output filenames automatically. The version is resolved in this priority order:

1. `--version-tag` CLI flag
2. `GITHUB_REF_NAME` environment variable (set automatically by GitHub Actions on tag push)
3. `VERSION` environment variable
4. `(rev ...)` field from your `.kicad_sch` title block
5. `(rev ...)` field from your `.kicad_pcb` title block

**Example** — push tag `v1.2.0` on a board named `my-board`:

```
my-board_v1.2.0.step
my-board_v1.2.0_gerbers.zip
my-board_v1.2.0_bom_jlc.csv
my-board_v1.2.0_cpl_jlc.csv
my-board_v1.2.0_sch.pdf
my-board_v1.2.0_3d_front.png
my-board_v1.2.0_3d_back.png
my-board_v1.2.0_front.svg
my-board_v1.2.0_back.svg
my-board_v1.2.0_ibom.html   ← <title> and in-page header also updated
```

---

## 📦 Interactive HTML BOM

KiForge uses [InteractiveHtmlBom](https://github.com/openscopeproject/InteractiveHtmlBom) to generate the `.html` BOM. If it isn't installed, KiForge will attempt to install it via `pip --user` at runtime.

To install manually:

```bash
# In GitHub Actions / Docker / standard CLI
pip install InteractiveHtmlBom

# KiCad GUI — Windows (KiCad Command Prompt)
pip install InteractiveHtmlBom

# KiCad GUI — macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
  -m pip install --user InteractiveHtmlBom

# KiCad GUI — Linux
pip3 install --user InteractiveHtmlBom
```

---

## 🔩 JLCPCB-ready outputs

### BOM (`*_bom_jlc.csv`)
- DNP (Do Not Populate) components are automatically filtered out
- LCSC part numbers are resolved from any of these custom fields: `LCSC`, `LCSC Part`, `LCSC Part #`, `JLCPCB Part`, `JLCPCB Part #`, `LCSC_Part`
- Output columns: `Designator`, `Comment`, `Footprint`, `LCSC`, `Quantity`

### CPL (`*_cpl_jlc.csv`)
- Output columns: `Designator`, `Mid X`, `Mid Y`, `Layer`, `Rotation`
- Component rotation offsets can be specified in a `.kiforge.json` file in your project root:

```json
{
  "rotation_offsets": {
    "SOT-23":    180,
    "QFN":       90,
    "USB-C":     270
  }
}
```

Patterns are matched against both the footprint package name and component value (case-insensitive).

---

## 📋 Log file

KiForge always writes a detailed log to `kiforge.log` inside your output directory.

```
[2026-05-29 13:33:07] [INFO]  Resolved project: my-board_v1.0.0 in /workspace
[2026-05-29 13:33:07] [INFO]  Running KiForge pipeline with 12 tasks.
[2026-05-29 13:33:08] [INFO]  Running command: kicad-cli pcb export gerbers ...
[2026-05-29 13:33:12] [INFO]  KiForge Exporter pipeline executed successfully.
```

The log includes resolved paths, every subprocess command with its full argument list, stdout/stderr from each step (at DEBUG level), and timestamps for every line.

---

## 🛠 Development

### Repository structure

```
kiforge/
├── kiforge.py              # Core exporter — single source of truth
├── action.yml              # GitHub Action composite definition
├── Dockerfile              # kicad/kicad:10.0-based image for CI
├── docker-compose.yml      # Local Docker test environment
├── package_plugin.py       # Builds the KiCad PCM plugin zip
├── metadata.json           # KiCad Plugin Manager manifest
├── plugins/
│   ├── __init__.py         # KiCad plugin registration hook
│   ├── kiforge_studio.py   # wx GUI (settings dialog + progress)
│   └── kiforge.py          # Auto-copied from root on packaging
├── tests/
│   ├── sample_project/     # Minimal KiCad project for CI tests
│   ├── test_cli.py         # Unit tests for CLI, context, versioning
│   └── test_studio.py      # Unit tests for GUI settings & CI gen
└── .github/workflows/
    ├── release.yml         # Builds versioned plugin zip on tag push
    └── test-action.yml     # Runs export on sample project (CI check)
```

### Running tests

```bash
python -m unittest tests/test_cli.py tests/test_studio.py -v
```

### Building the plugin package locally

```bash
# Unversioned (for local install / development)
python package_plugin.py

# Versioned release build (updates metadata.json automatically)
python package_plugin.py --version v0.2.0
```

Output: `dist/com.github.alphaseneca.kiforge-v0.2.0.zip`

### Adding a new export task

1. Subclass `ExportTask` in `kiforge.py` and implement `is_applicable()` and `run()`.
2. Register the task in `ExportRunner._initialize_pipeline()`.
3. Expose a matching `--[no-]export-<name>` CLI flag in `parse_cli_args()`.
4. Add the matching `export_<name>` input to `action.yml`.

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full description of the task pipeline, context model, and GUI threading design.

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">
  Made with ❤️ by <a href="https://ukesharyal.com.np">Ukesh Aryal</a>
</div>
