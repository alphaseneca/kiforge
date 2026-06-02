# KiForge

[![Test KiCad Exporter Action](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml/badge.svg)](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**KiForge** is an automated manufacturing files exporter for **KiCad 10** projects. Use it as:

- A **one-click KiCad Plugin** — runs exports directly from the PCB Editor with a progress dialog.
- A **reusable GitHub Action** — runs inside the official `kicad/kicad:10.0` Docker container on every tag push, producing release assets automatically.
- A **CLI script** — call `python kiforge.py` from any shell or Docker container.

It generates all files needed to order a board from JLCPCB and to document your design:

| Output | File | Flag |
|---|---|---|
| Gerber + Drill archive | `<name>_gerbers.zip` | `export_gerbers` / `export_drills` |
| JLCPCB Bill of Materials | `<name>_bom_jlc.csv` | `export_bom` |
| JLCPCB Component Placement | `<name>_cpl_jlc.csv` | `export_pos` |
| Schematic PDF | `<name>_sch.pdf` | `export_sch_pdf` |
| STEP 3D model | `<name>.step` | `export_step` |
| 3D front & back renders | `<name>_3d_front/back.png` | `export_3d` |
| Copper layer SVGs | `<name>_front/back.svg` | `export_svg` |
| Interactive HTML BOM | `ibom.html` | `export_ibom` |

---

## 1. KiCad 10 Action Plugin (GUI)

The plugin adds a **KiForge** entry under **Tools > External Plugins** inside the PCB Editor.

### Installation

Install via **KiCad Plugin Manager** (recommended) — search for `KiForge` — or copy the `plugins/` folder manually:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\kicad\10.0\scripting\plugins\com.github.alphaseneca.kiforge\` |
| macOS | `~/Library/Application Support/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/` |
| Linux | `~/.local/share/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/` |

### Usage

1. Open your `.kicad_pcb` file in KiCad 10.
2. Go to **Tools > External Plugins > KiForge**.
3. Select or confirm your project directory and output options.
4. Click **Run Export Now** — a progress dialog tracks each step.
5. All files are saved into the configured output folder (default: `kiforge/` next to your `.kicad_pro`).

The plugin also has a **Generate CI Files** button that writes a working GitHub Actions release workflow and updates your `.gitignore` automatically.

---

## 2. Reusable GitHub Action (CI/CD)

The action runs the exact same `kicad-cli` exports inside the official KiCad Docker container. Generated files are uploaded directly as GitHub Release assets — **nothing is committed to your repository**.

### Minimal Example

```yaml
name: Manufacturing Release

on:
  push:
    tags: ['v*']

permissions:
  contents: write

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

> Replace `alphaseneca/kiforge@v0.1.0` with the latest tag from the [KiForge releases page](https://github.com/alphaseneca/kiforge/releases).

### All Inputs

All inputs are optional. Every export is enabled by default — set to `'false'` to skip.

| Input | Description | Default |
|---|---|---|
| `project_path` | Path to the folder containing `.kicad_pro` | `'.'` |
| `output_dir` | Output folder name (relative to `project_path`) | `'kiforge'` |
| `export_gerbers` | Gerber layer files (zipped) | `'true'` |
| `export_drills` | Drill files (included in Gerber ZIP) | `'true'` |
| `export_bom` | JLCPCB-formatted Bill of Materials CSV | `'true'` |
| `export_pos` | JLCPCB-formatted Component Placement CSV | `'true'` |
| `export_sch_pdf` | Schematic PDF | `'true'` |
| `export_step` | STEP 3D model | `'true'` |
| `export_3d` | Front & back 3D renders (PNG) | `'true'` |
| `export_svg` | Front & back copper layer SVGs | `'true'` |
| `export_ibom` | Interactive HTML BOM | `'true'` |

For selective export examples and full workflow recipes, see [USAGE.md](USAGE.md).

---

## 3. Interactive HTML BOM (iBOM)

KiForge exports interactive BOMs using [InteractiveHtmlBom](https://github.com/openscopeproject/InteractiveHtmlBom). If the package is not installed, KiForge automatically tries to install it via `pip --user` at runtime.

To install manually:

```bash
# CLI / GitHub Actions
pip install InteractiveHtmlBom

# KiCad GUI — Windows (KiCad Command Prompt)
pip install InteractiveHtmlBom

# KiCad GUI — macOS
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m pip install --user InteractiveHtmlBom

# KiCad GUI — Linux
pip3 install --user InteractiveHtmlBom
```

---

## 4. Local Testing with Docker Compose

Test against the same KiCad Docker environment used by the GitHub Action:

```bash
docker compose run --rm export
```

By default this runs against `tests/sample_project` and writes output to `tests/sample_project/kiforge/`.

---

## License

MIT — see [LICENSE](LICENSE).
