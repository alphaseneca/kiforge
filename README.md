# KiForge

[![Test KiCad Exporter Action](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml/badge.svg)](https://github.com/alphaseneca/kiforge/actions/workflows/test-action.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**KiForge** is an automated manufacturing files exporter for **KiCad 10** projects. It allows developers to run standard manufacturing and documentation exports with one click from inside the KiCad PCB editor, or automatically as part of a GitHub CI/CD pipeline using the official KiCad Docker environment.

It generates:
* **Gerber & Drill ZIP**: All board layer plots and plated/non-plated drill files zipped into a single archive (`_gerbers.zip`) aligned for JLCPCB.
* **JLCPCB BOM**: Clean CSV Bill of Materials optimized with JLCPCB columns and LCSC parts (`_bom_jlc.csv`).
* **JLCPCB CPL**: Component Placement List (position file) aligned for JLCPCB SMT assembly (`_cpl_jlc.csv`).
* **3D renders**: Front and back high-quality perspectives (`.png`)
* **SVGs**: Vector exports of front/back copper layers and board edges (`.svg`)
* **Schematic PDF**: Complete multi-sheet schematics compiled into a single document (`_sch.pdf`)
* **STEP Model**: 3D step file for mechanical alignment and enclosure design (`.step`)

---

## 1. KiCad 10 Action Plugin (GUI)

The Python Action Plugin adds a **KiForge** menu item under **Tools > External Plugins** inside the PCB Editor. It runs the exports in a subprocess using `kicad-cli` and displays a progress bar.

### Installation

Copy the `plugins/` directory and `metadata.json` into your local KiCad 10 scripting plugins folder:

* **Windows**: `%APPDATA%\kicad\10.0\scripting\plugins\com.github.alphaseneca.kiforge\`
* **macOS**: `~/Library/Application Support/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/`
* **Linux**: `~/.local/share/kicad/10.0/scripting/plugins/com.github.alphaseneca.kiforge/`

### Usage

1. Open your project PCB file (`.kicad_pcb`) in KiCad 10.
2. Select **Tools > External Plugins > KiForge**.
3. A progress dialog will show the status of the exports.
4. All exports are saved to a folder named `kiforge/` located in the same directory as the active `.kicad_pro` project file.

---

## 2. Reusable GitHub Action (CI/CD)

The GitHub Action runs inside the official `kicad/kicad:10.0` container, running identical CLI exports in a clean, reproducible environment.

### Example Workflow

Create a file named `.github/workflows/export-pcb.yml` in your project repository:

```yaml
name: Export PCB Manufacturing Files

on:
  push:
    branches: [ main ]
  pull_request:

jobs:
  export:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Run KiForge Exporter
        uses: alphaseneca/kiforge@v1
        with:
          project_path: '.'  # Directory containing .kicad_pro
          output_dir: 'kiforge'

      - name: Upload Manufacturing Files
        uses: actions/upload-artifact@v4
        with:
          name: board-manufacturing-files
          path: kiforge/
```

### Action Inputs

| Input | Description | Default | Required |
| :--- | :--- | :--- | :--- |
| `project_path` | Relative path to the folder containing `.kicad_pro` | `.` | Yes |
| `output_dir` | Name of directory (relative to `project_path`) where files are saved | `kiforge` | No |
| `export_3d` | Toggle front/back 3D renders (`true` / `false`) | `true` | No |
| `export_svg` | Toggle copper layer SVG exports (`true` / `false`) | `true` | No |
| `export_bom` | Toggle BOM CSV generation (`true` / `false`) | `true` | No |
| `export_sch_pdf` | Toggle schematic PDF generation (`true` / `false`) | `true` | No |
| `export_pos` | Toggle position placement file CSV (`true` / `false`) | `true` | No |
| `export_step` | Toggle STEP 3D model generation (`true` / `false`) | `true` | No |
| `export_gerbers` | Toggle Gerber files generation (`true` / `false`) | `true` | No |
| `export_drills` | Toggle Drill files generation (`true` / `false`) | `true` | No |
| `export_ibom` | Toggle Interactive HTML BOM generation (`true` / `false`) | `true` | No |

---

## 3. Interactive HTML BOM (iBOM) Integration

KiForge supports exporting interactive HTML BOMs using the popular [InteractiveHtmlBom](https://github.com/openscopeproject/InteractiveHtmlBom) tool. 

To enable this feature, the `InteractiveHtmlBom` Python package must be installed in the active Python environment. If it is missing, KiForge will attempt to install it automatically via `pip` under the user context.

### Manual Installation

If you prefer to install it manually:

*   **GitHub Actions / CLI**: The action automatically installs the dependency via pip at runtime. If you run locally, install it in your environment:
    ```bash
    pip install InteractiveHtmlBom
    ```
*   **KiCad GUI (Windows)**: Open the **KiCad Command Prompt** (from the start menu) and run:
    ```cmd
    pip install InteractiveHtmlBom
    ```
    *(Alternatively: `kicad-python.exe -m pip install --user InteractiveHtmlBom`)*
*   **KiCad GUI (macOS)**: Run terminal command using the KiCad-bundled Python:
    ```bash
    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m pip install --user InteractiveHtmlBom
    ```
*   **KiCad GUI (Linux)**: Install using your system's python package manager:
    ```bash
    pip3 install --user InteractiveHtmlBom
    ```

---

## 3. Local Testing with Docker Compose

To test the exporter locally using the exact same environment as the GitHub Action:

1. Install [Docker](https://www.docker.com/) and [Docker Compose](https://docs.docker.com/compose/).
2. Run the compose service:
   ```bash
   docker compose run --rm export
   ```
3. By default, this will run exports against the `tests/sample_project` folder and output results to `tests/sample_project/kiforge/`.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
