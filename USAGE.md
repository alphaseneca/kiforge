# KiForge — Usage Guide

**KiForge** runs inside the official **KiCad 10 Docker image** to automatically export manufacturing and documentation files from your KiCad project on every tag push.

> **Your repository stays clean.** KiForge runs on a temporary GitHub Actions runner. It generates files there, uploads them directly to a GitHub Release as downloadable assets, and the runner is discarded. Nothing is ever committed.

---

## Quick Start

Create `.github/workflows/release.yml` in your KiCad project repository:

```yaml
name: Manufacturing Release

on:
  push:
    tags:
      - 'v*'   # Triggers on tags like v0.1.0, v0.2.0, etc.

permissions:
  contents: write   # Required to create GitHub Releases and upload assets

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

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

> Replace `alphaseneca/kiforge@v0.1.0` with the latest tag from the [KiForge releases page](https://github.com/alphaseneca/kiforge/releases).

---

## All Available Inputs

All inputs are optional. Every export is enabled by default. Set an input to `'false'` to disable it.

| Input | Description | Default |
|---|---|---|
| `project_path` | Relative path to your KiCad project directory (containing `.kicad_pro`) | `'.'` |
| `output_dir` | Directory where output files are saved (relative to `project_path`) | `'kiforge'` |
| `export_gerbers` | Export Gerber layer files (zipped) | `'true'` |
| `export_drills` | Export drill files (included in Gerber ZIP) | `'true'` |
| `export_bom` | Export JLCPCB-formatted Bill of Materials CSV | `'true'` |
| `export_pos` | Export JLCPCB-formatted component placement CSV | `'true'` |
| `export_sch_pdf` | Export schematic as a PDF | `'true'` |
| `export_step` | Export STEP 3D model | `'true'` |
| `export_3d` | Export front & back 3D PNG renders | `'true'` |
| `export_svg` | Export front & back copper layer SVGs | `'true'` |
| `export_ibom` | Export Interactive HTML BOM | `'true'` |
| `format_jlc` | Apply JLCPCB BOM/CPL column formatting | `'true'` |
| `version` | Override version suffix for output filenames | _(auto from Git tag)_ |

> **Version tagging:** All output filenames are versioned automatically on every run — locally and in CD. On tag push, KiForge reads `GITHUB_REF_NAME` (e.g. `myboard_v1.0.0_gerbers.zip`, `myboard_v1.0.0_sch.pdf`). Locally, the version comes from `--version-tag`, title-block `(rev ...)`, or defaults to `v0.1.0`.

---

## Output Files

KiForge writes all files into `output_dir/` on the GitHub Actions runner — not in your repository. The upload step then attaches them as downloadable assets to your GitHub Release.

`<name>` is derived from your `.kicad_pro` / `.kicad_pcb` filename plus the resolved version suffix (e.g. `myboard_v1.0.0`).

| File | Description | Controlled by |
|---|---|---|
| `<name>_gerbers.zip` | Gerber + Drill files archive | `export_gerbers` / `export_drills` |
| `<name>_bom_jlc.csv` | JLCPCB Bill of Materials | `export_bom` |
| `<name>_cpl_jlc.csv` | JLCPCB Component Placement List | `export_pos` |
| `<name>_sch.pdf` | Schematic PDF | `export_sch_pdf` |
| `<name>.step` | STEP 3D model | `export_step` |
| `<name>_3d_front.png` | 3D front render | `export_3d` |
| `<name>_3d_back.png` | 3D back render | `export_3d` |
| `<name>_front.svg` | Front copper layer SVG | `export_svg` |
| `<name>_back.svg` | Back copper layer SVG | `export_svg` |
| `<name>_ibom.html` | Interactive HTML BOM | `export_ibom` |

---

## Selective Export Examples

### JLCPCB Fabrication Only

Exports only what JLCPCB needs to manufacture and assemble your board: Gerbers, Drills, BOM, and CPL.

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

---

### Documentation Only

Exports the schematic PDF, 3D renders, and SVGs — no fabrication data.

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

---

### Interactive BOM + Gerbers Only

Useful for sharing a reviewable board layout alongside fabrication files.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          export_3d: 'false'
          export_svg: 'false'
          export_bom: 'false'
          export_sch_pdf: 'false'
          export_pos: 'false'
          export_step: 'false'
```

---

### Project in a Subdirectory

If your KiCad project is not in the repository root:

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: 'hardware/my-board'
          output_dir: 'hardware/my-board/kiforge'
```

---

## Complete Release Workflow

Full production workflow — exports everything and uploads all files as GitHub Release assets.

```yaml
name: JLCPCB Manufacturing Release

on:
  push:
    tags:
      - 'v*'

permissions:
  contents: write

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Run KiForge Exporter
        uses: alphaseneca/kiforge@v0.1.0
        with:
          project_path: '.'
          output_dir: 'kiforge'

      - name: Create GitHub Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: kiforge/*
```

---

## How to Trigger a Release

Once the workflow file is in your repository, push a Git tag:

```bash
git tag -a v0.1.0 -m "First release"
git push origin v0.1.0
```

GitHub Actions will:
1. Check out your repository
2. Launch the KiCad 10 Docker container
3. Run KiForge to generate all manufacturing files
4. Create a GitHub Release with auto-generated release notes
5. Upload all generated files as downloadable release assets

---

## Log File

KiForge writes a detailed log to `kiforge.log` inside your output directory.

The log contains:
- Resolved paths (KiCad CLI, Python interpreter, project files)
- Every command executed with its full argument list
- `stdout`/`stderr` of each subprocess (at DEBUG level)
- Timestamps and source locations for every line

Example:
```
[2026-05-29 13:33:07] [INFO] [KiForge.Core:kiforge.py] Resolved project: my-board in /workspace
[2026-05-29 13:33:07] [INFO] [KiForge.Core:kiforge.py] Running KiForge pipeline with 12 tasks.
[2026-05-29 13:33:08] [INFO] [KiForge.Core:kiforge.py] Running command: kicad-cli pcb export gerbers ...
[2026-05-29 13:33:12] [INFO] [KiForge.Core:kiforge.py] KiForge Exporter pipeline executed successfully.
```
