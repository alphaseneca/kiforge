# KiForge — Usage Guide

**KiForge** runs inside the official **KiCad 10 Docker image** to automatically export manufacturing and documentation files from your KiCad project on every tag push.

> **Your repository stays clean.** KiForge runs on a temporary GitHub Actions runner. It generates files there, uploads them directly to a GitHub Release as downloadable assets, and the runner is discarded. Nothing is ever committed.

> **Runner requirement:** this Action builds and runs a Docker image, so it needs a **Linux runner with Docker** — `runs-on: ubuntu-latest` (as below), a Linux self-hosted runner, or a Linux Gitea Actions runner. GitHub-hosted `macos-*` runners do not provide Docker; use the KiCad Studio plugin locally on macOS instead (see the main [README](README.md)).

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
        uses: alphaseneca/kiforge@vX.Y.Z
        with:
          project_path: '.'

      - name: Create Release and Upload Assets
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: kiforge/*
```

> If you install KiForge from PCM, **Generate CD Files** in Studio writes `uses: alphaseneca/kiforge@vX.Y.Z` for the plugin version you installed. When editing YAML by hand, pin the same [release tag](https://github.com/alphaseneca/kiforge/releases) as your KiForge plugin — do not use `@main`.

---

## All Available Inputs

All inputs are optional. Every export is enabled by default. Set an input to `'false'` to disable it.

| Input | Description | Default |
|---|---|---|
| `project_path` | Relative path to your KiCad project directory (containing `.kicad_pro`) | `'.'` |
| `output_dir` | Directory where output files are saved (relative to `project_path`) | `'kiforge'` |
| `export_gerbers` | Export Gerber layer files (zipped) | `'true'` |
| `export_drills` | Export drill files (included in Gerber ZIP) | `'true'` |
| `export_bom` | Export Bill of Materials CSV (KiCad raw + optional JLC copy) | `'true'` |
| `export_pos` | Export component placement CSV (KiCad raw + optional JLC copy) | `'true'` |
| `export_sch_pdf` | Export schematic as a PDF | `'true'` |
| `export_step` | Export STEP 3D model | `'true'` |
| `export_3d` | Export front & back 3D PNG renders | `'true'` |
| `export_svg` | Export front & back copper layer SVGs | `'true'` |
| `export_print_pdf` | Export 1200 DPI print-ready etching & mask PDF (1:1 true scale) | `'true'` |
| `export_ibom` | Export Interactive HTML BOM | `'true'` |
| `format_jlc` | Also produce JLC-ready BOM/CPL from KiCad CSV exports | `'true'` |
| `pos_side` | Placement CSV side: `both`, `front` (top), or `back` (bottom) | `'both'` |
| `pos_smd_only` | Placement CSV: SMD parts only | `'true'` |
| `pos_exclude_dnp` | Placement CSV: exclude DNP parts | `'true'` |
| `step_subst_models` | STEP export: substitute missing 3D models | `'true'` |
| `bom_include_mfr_mpn` | BOM/iBOM: include Manufacturer & MPN columns | `'true'` |
| `sync_title_block_rev` | Sync schematic title-block `(rev …)` to the export version | `'true'` |
| `version` | Override version suffix for output filenames | _(auto from Git tag)_ |

> **Export parameters:** The `pos_*`, `step_*`, and `bom_*` inputs map to `export_params` in `.kiforge.json`. Gerber/drill layers and 3D render quality are fixed (`GERBER_EXPORT_DEFAULTS`, `DRILL_EXPORT_DEFAULTS`, `RENDER_3D_DEFAULTS`). BOM fields and iBOM grouping mirror `BOM_EXPORT_DEFAULTS` (with Manufacturer and MPN columns toggled on/off dynamically via the `bom_include_mfr_mpn` flag). Raw `*_bom.csv` includes `ID` and `MPN`; JLC copies are produced by `JLCPCBFormatter` when `format_jlc` is on.

> **3D Model Resolution & Multi-Stage Rendering:** KiForge always sets `KIPRJMOD` to the resolved project directory, so footprints that bundle custom 3D models with the project resolve reliably via `${KIPRJMOD}/<relative-path>` — on every OS, and in CD/Docker where nothing outside the checked-out repo is guaranteed to exist. Separately, KiForge resolves `KICAD10_3DMODEL_DIR` (and the `KISYS3DMOD`/`KICAD{7,8,9}_3DMODEL_DIR` aliases) to KiCad's *official* system 3D library, derived from the running `kicad-cli` binary's own install location — never from a project folder, since standard KiCad footprints depend on that variable pointing at the real library. 3D rendering also employs a multi-stage fallback ladder: if high-quality raytracing (`--preset 2`) fails due to VRML (`.wrl`) mesh parse errors, missing models, or headless environment limits, KiForge automatically falls back to standard rasterization (`--preset 0`) so that complete board renders with all available SMD 3D models are always preserved.
>
> **No 3D library in the Docker image by default:** the official `kicad/kicad` image KiForge's Action/CD pipeline runs in ships without KiCad's 3D model library — it's roughly 10x the base image size, so upstream excludes it. KiForge installs it automatically at the start of every run if it isn't already there (`kicad-packages3d`, ~5 minutes on a cold container), so standard-library parts (resistors, capacitors, connectors, ...) get complete STEP/3D-render output with no configuration needed. If that install ever fails (offline runner, blocked package mirror, non-root container), KiForge logs a warning and continues — the export still succeeds, just with STEP/3D-render output that has no 3D body for the affected parts, since kicad-cli degrades gracefully rather than failing outright. Project-bundled models via `${KIPRJMOD}/...` are unaffected either way.
>
> **Tip:** if your project ships its own custom 3D models (parts not in KiCad's standard library), point the footprint's 3D model field at `${KIPRJMOD}/3dmodels/your-part.step` (or wherever you keep them relative to the project root) rather than an absolute path — that is the only reference style guaranteed to resolve identically on your machine and inside the CI/CD container.

> **Version tagging:** All output filenames are versioned automatically on every run — locally and in CD. On tag push, KiForge reads `GITHUB_REF_NAME` (e.g. `myboard_v1.0.0_gerbers.zip`, `myboard_v1.0.0_sch.pdf`). Locally, the version comes from `--version-tag`, the latest git tag (when enabled), or defaults to `v0.1.0`. During export only, schematic PDFs sync the title-block `(rev …)` to that version via a temporary staged copy (`--sync-title-block-rev`, on by default in CI and Studio Run Export).

---

## Output Files

KiForge writes all files into `output_dir/` on the GitHub Actions runner — not in your repository. The upload step then attaches them as downloadable assets to your GitHub Release.

`<name>` is derived from your `.kicad_pro` / `.kicad_pcb` filename plus the resolved version suffix (e.g. `myboard_v1.0.0`).

| File | Description | Controlled by |
|---|---|---|
| `<name>_gerbers.zip` | Gerber + Drill files archive | `export_gerbers` / `export_drills` |
| `<name>_bom.csv` | KiCad Bill of Materials (raw) | `export_bom` |
| `<name>_bom_jlc.csv` | JLCPCB Bill of Materials | `export_bom` + `format_jlc` |
| `<name>_pos.csv` | KiCad placement CSV (raw) | `export_pos` |
| `<name>_cpl_jlc.csv` | JLCPCB Component Placement List | `export_pos` + `format_jlc` |
| `<name>_sch.pdf` | Schematic PDF | `export_sch_pdf` |
| `<name>.step` | STEP 3D model | `export_step` |
| `<name>_3d_front.png` | 3D front render | `export_3d` |
| `<name>_3d_back.png` | 3D back render | `export_3d` |
| `<name>_front.svg` | Front copper layer SVG (negative B&W) | `export_svg` |
| `<name>_back.svg` | Back copper layer SVG (mirrored negative B&W) | `export_svg` |
| `<name>_homebrew.svg` | Merged A4 homebrew etching sheet with calibration scale & optical fiducials | `export_svg` |
| `<name>_homebrew.pdf` | 1200 DPI homebrew etching & mask PDF (1:1 true scale) | `export_print_pdf` |
| `<name>_ibom.html` | Interactive HTML BOM | `export_ibom` |
| `<name>_pos_neoden.csv` | NeoDen formatted pick-and-place CSV | `templates/gitea-pnp-neoden.yml` |
| `<name>_pnp_ref_origin.png` | Front-copper optical alignment image with origin crosshairs | `templates/gitea-pnp-neoden.yml` |

---

## NeoDen Pick-and-Place Post-Processing

For automated SMT assembly on NeoDen pick-and-place machines (e.g. NeoDen 4, NeoDen 9, NeoDen YY1), KiForge includes a dedicated post-process workflow template: `templates/gitea-pnp-neoden.yml`.

### Workflow Features:
1. **Direct Extraction from PCB:** Runs `kicad-cli pcb export pos` inside KiCad 10 Docker directly on the checked-out `.kicad_pcb`.
2. **Config-Aware:** Reads `.kiforge.json` (or `.kiforge.json` in the PCB folder) for `pos_side` (`both`, `front`, `back`), `pos_smd_only`, and `pos_exclude_dnp`.
3. **Format Conversion:** Converts position data to NeoDen column specifications:
   `Designator, Footprint, Mid X, Mid Y, Layer, Rotation, Comment` (Layer formatted as `T` / `B`).
4. **Optical Reference Image:** Renders a high-resolution front-copper alignment PNG with optical crosshairs at `aux_axis_origin` (drill/place origin) and `grid_origin` for machine fiducial/board calibration.
5. **Tag Release Attachment:** Uploads both files directly to the tag release without overwriting other release artifacts.

To enable in your project:
```bash
# Copy template into your project's workflow directory
cp templates/gitea-pnp-neoden.yml <your-kicad-project>/.gitea/workflows/pnp-neoden.yml
```

---

## JLC-ready BOM/CPL

When `format_jlc` is enabled (default), KiForge exports with `kicad-cli`, then `JLCPCBFormatter` writes JLC-upload copies ([JLCPCB KiCad Method 1](https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad)).

**Raw BOM columns:** `Reference`, `Value`, `Footprint`, `Description`, `${QUANTITY}`, `${DNP}`, `ID`, `MPN`

| KiCad export | JLC file | Mapping |
|---|---|---|
| `*_bom.csv` | `*_bom_jlc.csv` | Value→Comment, Reference→Designator, `ID`→`LCSC Part #` when `^C\d+$`, `${QUANTITY}`→Quantity |
| `*_pos.csv` | `*_cpl_jlc.csv` | Ref→Designator, PosX/Y→Mid X/Y, Rot→Rotation, Side→Layer |

Placement uses CSV, millimetres, drill-file origin, and configurable side (`both`, `front`, `back`), SMD, and DNP via `export_params`. Add **ID** on symbols for JLC numbers (`C125111`); **MPN** for manufacturer data in the raw BOM only.

Disable JLC copies only: `format_jlc: false` or `--no-format-jlc`.

---

## KiForge Studio (GUI plugin)

The PCM plugin opens **KiForge Studio** — a tabbed dialog:

| Tab | Purpose |
| --- | --- |
| **Export** | Project folder, output name, presets, live summary |
| **Advanced** | Individual output toggles, placement sides (`Both`, `Front`, `Back`), SMD/DNP options, BOM columns |
| **Releases** | CD workflow generation ("Set up workflows") and live auto-sync |

Tab icons load from Google Material Symbols CDN once and cache under `%APPDATA%/kiforge/icon_cache/` (or platform equivalent).

---

### JLCPCB Fabrication Only

Exports only what JLCPCB needs to manufacture and assemble your board: Gerbers, Drills, BOM, and CPL.

```yaml
      - name: Run KiForge
        uses: alphaseneca/kiforge@vX.Y.Z
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
        uses: alphaseneca/kiforge@vX.Y.Z
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
        uses: alphaseneca/kiforge@vX.Y.Z
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
        uses: alphaseneca/kiforge@vX.Y.Z
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
        uses: alphaseneca/kiforge@vX.Y.Z
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

## CLI (`python kiforge.py`)

The CLI exposes the same three configuration layers as the GitHub Action:

| Layer | CLI flags | Persisted in `.kiforge.json` |
| --- | --- | --- |
| Export toggles | `--export-bom` / `--no-export-bom`, … | `exports` |
| Export parameters | `--pos-side`, `--top`, `--bottom`, … | `export_params` |
| Runtime | `--sync-title-block-rev` / `--no-sync-title-block-rev` | _(not saved)_ |

Examples:

```bash
# JLC-oriented placement (top only, SMD, no DNP) and unoptimized STEP
python kiforge.py --top --pos-smd-only --pos-exclude-dnp

# Bottom-side placement only
python kiforge.py --bottom

# Generate CD workflow YAML from current CLI selections
python kiforge.py --generate-cd --no-export-3d --pos-side back
```

When export-parameter flags are omitted, KiForge merges defaults with project/global settings from `.kiforge.json` (Studio **Save** writes those files).

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
