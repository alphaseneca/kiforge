# KiForge — GitHub Action Usage Guide

**KiForge** is a GitHub Action that runs inside the official **KiCad 10 Docker image** to automatically export manufacturing and documentation files from your KiCad project on every tag push.

> **The generated files never get committed to your repository.** KiForge runs inside a temporary GitHub Actions runner. It generates the files there, uploads them directly to your GitHub Release as downloadable assets, and the runner is then discarded. Your repo stays clean — only source files live there.

---

## Quick Start

Create `.github/workflows/release.yml` in your KiCad project repository:

```yaml
name: Manufacturing Release

on:
  push:
    tags:
      - 'v*'   # Triggers on tags like v1.0.0, v2.3.1, etc.

permissions:
  contents: write   # Required to create GitHub Releases and upload assets

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Run KiForge
        uses: alphaseneca/kiforge@v1.0.0
        with:
          project_path: '.'

      - name: Create Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: kiforge/*   # Upload every generated file directly as a release asset
```

> **Note**: Replace `alphaseneca/kiforge@v1.0.0` with the latest published tag from the [KiForge releases page](https://github.com/alphaseneca/kiforge/releases).

---

## All Available Inputs

All inputs below are optional. Every export is enabled by default. Set an input to `'false'` to disable it.

| Input          | Description                                      | Default  |
|----------------|--------------------------------------------------|----------|
| `project_path` | Relative path to your KiCad project directory (containing `.kicad_pro`) | `'.'`    |
| `output_dir`   | Directory where output files are saved           | `'kiforge'` |
| `export_3d`    | Export front & back 3D PNG renders               | `'true'` |
| `export_svg`   | Export front & back copper layer SVGs            | `'true'` |
| `export_bom`   | Export JLCPCB-formatted Bill of Materials CSV    | `'true'` |
| `export_sch_pdf` | Export schematic as a PDF                      | `'true'` |
| `export_pos`   | Export JLCPCB-formatted component placement CSV  | `'true'` |
| `export_step`  | Export STEP 3D model file                        | `'true'` |
| `export_gerbers` | Export Gerber layer files (zipped)             | `'true'` |
| `export_drills`  | Export drill files (included in Gerber ZIP)    | `'true'` |
| `export_ibom`  | Export Interactive HTML BOM (via `InteractiveHtmlBom`) | `'true'` |

---

## Output Files

KiForge generates files into a temporary `output_dir` folder (`kiforge/` by default) **on the GitHub Actions runner — not in your repository**. The upload step then picks them up and attaches them as downloadable assets to your GitHub Release. Once the runner finishes, everything is discarded. Nothing is ever committed.

The `kiforge/` folder is also listed in `.gitignore` so even if you run KiForge locally, the output won't accidentally get staged or committed.

| File                        | Description                       | Enabled by          |
|-----------------------------|-----------------------------------|---------------------|
| `<name>_gerbers.zip`        | Gerber + Drill files archive      | `export_gerbers`    |
| `<name>_bom_jlc.csv`        | JLCPCB Bill of Materials          | `export_bom`        |
| `<name>_cpl_jlc.csv`        | JLCPCB Component Placement List   | `export_pos`        |
| `<name>_sch.pdf`            | Schematic PDF                     | `export_sch_pdf`    |
| `<name>.step`               | STEP 3D model                     | `export_step`       |
| `ibom.html`                 | Interactive HTML BOM              | `export_ibom`       |
| `<name>_3d_front.png`       | 3D front render                   | `export_3d`         |
| `<name>_3d_back.png`        | 3D back render                    | `export_3d`         |
| `<name>_front.svg`          | Front copper layer SVG            | `export_svg`        |
| `<name>_back.svg`           | Back copper layer SVG             | `export_svg`        |

---

## Selective Export Examples

### JLCPCB Fabrication Only (No 3D or PDFs)

Exports only what JLCPCB needs: Gerbers, Drills, BOM, and CPL.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v1.0.0
        with:
          project_path: '.'
          export_3d: 'false'
          export_svg: 'false'
          export_sch_pdf: 'false'
          export_step: 'false'
          export_ibom: 'false'
```

---

### Documentation Only (No Fabrication Files)

Exports only the schematic PDF, 3D renders, and SVGs — no fabrication data.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v1.0.0
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

Useful for sharing a reviewable board layout link alongside fabrication files.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@v1.0.0
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
        uses: alphaseneca/kiforge@v1.0.0
        with:
          project_path: 'hardware/my-board'
          output_dir: 'hardware/my-board/kiforge'
```

---

## Complete Release Workflow (JLCPCB-Ready)

Full production workflow that exports everything and uploads all files as GitHub Release assets. **None of these files are committed to your repository** — they exist only on the runner during the workflow run and are then published to the Release.

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
        uses: alphaseneca/kiforge@v1.0.0
        with:
          project_path: '.'
          output_dir: 'kiforge'
          export_3d: 'true'
          export_svg: 'true'
          export_bom: 'true'
          export_sch_pdf: 'true'
          export_pos: 'true'
          export_step: 'true'
          export_gerbers: 'true'
          export_drills: 'true'
          export_ibom: 'true'

      - name: Create GitHub Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          # All generated files are uploaded directly as release assets.
          # They are never committed to the repository.
          files: kiforge/*
```

---

## How to Trigger a Release

Once you have the workflow file in your repository, push a Git tag to trigger it:

```bash
# Tag your current commit
git tag -a v1.0.0 -m "First production release"

# Push the tag to GitHub
git push origin v1.0.0
```

GitHub Actions will:
1. Check out your repository
2. Launch the KiCad 10 Docker container
3. Run KiForge to generate all manufacturing files
4. Create a GitHub Release named `v1.0.0` with auto-generated release notes
5. Upload all generated files as downloadable release assets

---

## Log File

KiForge writes a detailed log to `kiforge/kiforge.log` in your output directory.
This log contains:
- Environment diagnostics (Python path, platform, site-packages)
- Each command executed with its arguments
- `stdout`/`stderr` of subprocesses at DEBUG level
- Timestamps and source locations for every log line

```
[2026-05-29 13:33:07] [INFO] [KiForge.Core:kiforge.py:293] Initialized KiForge Exporter for project: my-board
[2026-05-29 13:33:08] [INFO] [KiForge.Core:kiforge.py:474] [1/12] Running command: kicad-cli pcb export gerbers ...
[2026-05-29 13:33:09] [INFO] [KiForge.Core:kiforge.py:503] [2/12] Zipping Gerber and Drill files...
```
