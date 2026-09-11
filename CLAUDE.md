# KiForge — working agreements

KiForge is a KiCad 10 plugin and CLI exporter. It runs in three places with
different constraints: inside KiCad's bundled Python (Studio), under a system
Python (CLI), and inside a headless Docker container (the GitHub Action).
Almost every rule below exists because one of those three broke.

Adapted from the [KiCad developer rules and
guidelines](https://dev-docs.kicad.org/en/rules-guidelines/index.html) where
they transfer. Those are written for KiCad's C++ application code; what carries
over is the reasoning, not the syntax.

---

## 1. The interpreter floor is 3.9 — this is the one that bites

KiCad bundles its own Python and **the version differs per platform**: macOS
ships 3.9.13 inside `KiCad.app`, other platforms ship newer 3.x. The supported
interpreter is a range with a floor, and every shipped module must run across
all of it.

The trap: `str | None` (PEP 604) is valid *syntax* on 3.9 but is evaluated at
`def` time, so it raises `TypeError` on import. Inside KiCad that is invisible —
PCM reports the package installed and no toolbar button ever appears.

- Shipped modules carry `from __future__ import annotations`.
- Do not use syntax or stdlib APIs newer than 3.9 (`match`, `tomllib`,
  `zip(strict=)`, `X | Y` at runtime).
- The floor is declared in four places that must agree:
  `plugins/__init__.py:MIN_PYTHON`, `pyproject.toml` `[tool.kiforge] min-python`,
  `[tool.ruff] target-version`, and the CI matrix.
  `tests/test_python_compat.py` fails if they drift.

## 2. Run these before saying it works

```bash
python tests/kicad_runtime_stub.py                       # load gate, no deps
python -m unittest discover -s tests -p 'test_*.py'      # headless suite
ruff check .                                             # FA102 compat rules
```

The strongest local check is the load gate under **KiCad's own Python**, which
is what the plugin actually runs on:

```bash
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 tests/kicad_runtime_stub.py
```

GUI tests need `KIFORGE_RUN_GUI_TESTS=1` and real wx, so run them with KiCad's
Python too.

**Gotcha:** `package_plugin.py` copies the root `kiforge.py` to
`plugins/kiforge.py` and leaves it there for local plugin development.
`kiforge_studio`'s `from . import kiforge` then binds to that stale copy instead
of the module you just edited, and your change appears to do nothing. Delete it
(`rm plugins/kiforge.py`) before testing Studio. `tests/test_studio.py` detects
this and skips with the remedy; ad-hoc scripts do not.

## 3. Dependencies: resolve them lazily, at the point of need

**PCM has no install hook.** Verified against the [KiCad addon
specification](https://dev-docs.kicad.org/en/addons/index.html): a package is a
plain zip extracted to fixed locations, with no install script, no post-install
hook, and no way to declare Python dependencies. The `runtime` field selects
`ipc` vs `swig`, nothing more. The earliest code KiCad runs is
`plugins/__init__.py` at plugin-scan time, and pip-installing from there would
block KiCad's startup on a network download.

So a missing package is resolved **where it is first needed**, inside the export
task, through the cancellable `_run_subprocess` runner so progress reports and
Cancel keep working. `IbomExportTask` and
`HomebrewPdfExportTask._ensure_renderer_installed` share one ladder,
`pip_install_attempts()`: `--user`, then `--user --force-reinstall`, then
`--target get_package_dir()`. Best-effort, and the failure path names the
remedy.

Two rules the ladder exists to enforce:

- **Success is the import, never pip's exit code.** pip answers "Requirement
  already satisfied" from metadata on disk, so an orphaned `.dist-info` makes
  every install a silent no-op while the import keeps failing — and the next
  export tries again, forever. Re-probe the target interpreter after each
  attempt.
- **Never `--break-system-packages`.** It lifts PEP 668's guard for the whole
  environment; a plugin wanting two optional packages should not be making
  that promise. `--target get_package_dir()` writes into a directory KiForge
  owns and nothing else reads, so it cannot shadow a distribution package and
  uninstalling is deleting the folder. Every interpreter KiForge drives is
  told about it — `package_dir_path_snippet()` for a `-c` snippet,
  `add_package_dir_to_path()` in-process.

Install into **KiCad's interpreter**, not the running one. Inside the KiCad GUI
`sys.executable` is the application binary, not Python — use
`PathResolver.get_kicad_python_path()`, and probe that same interpreter when
deciding what is missing.

Anything that is a full desktop application is not a dependency. Inkscape was
dropped for this reason.

## 4. Assets ship with the plugin

Studio's icons live in `icons/` and are packaged to `plugins/icons/`. Do not
fetch UI assets at render time: KiCad's macOS Python is a python.org framework
build with **no CA store**, so every HTTPS request fails with
`CERTIFICATE_VERIFY_FAILED` and the UI silently renders blank. A plugin must not
need the network to draw itself.

When a network call is genuinely required, get the context from
`kiforge._https_context()`, which falls back to the `certifi` bundle KiCad ships
when the default trust store is empty.

## 5. Style

Python conventions (PEP 8) rather than KiCad's C++ casing. What carries over
from the [code style
policy](https://dev-docs.kicad.org/en/rules-guidelines/code-style/index.html):

- **Four spaces, never tabs.** Soft limit 120 columns; the long lines already in
  `kiforge.py` are legacy, not licence.
- **Self-documenting code.** "Avoid comments that state what code does. It should
  be obvious what code does or it should be rewritten so that it is obvious."
- **Comments explain *why*.** This codebase's house style is a short paragraph
  above non-obvious code recording the constraint that forced it — which
  platform, which failure, what was tried. That context is the most valuable
  thing in the file; match its density when you add code.
- **Docstrings**: one-line summary, blank line, detail. `Returns:` where the
  return shape is not obvious. Do not restate the signature.
- **Be explicit, never implicit** — the through-line of KiCad's
  [anti-patterns](https://dev-docs.kicad.org/en/rules-guidelines/anti-patterns/index.html).
  Parse a token and discard it rather than skipping by count; write `yes`/`no`
  rather than relying on presence.
- **Naming conventions across interfaces**:
  - **CLI flags**: always kebab-case (`--pcb-file`, `--project-path`, `--output-dir`, `--export-gerbers`).
  - **GitHub Action inputs**: kebab-case (`pcb-file`, `project-path`, `output-dir`, `export-gerbers`), matching CLI flags 1:1.
  - **Python code & internals**: PEP 8 snake_case (`pcb_file`, `project_path`, `output_dir`).

## 6. Failure must be visible

KiCad's plugin loader wraps each import in a bare `except:` and records the
traceback only in `pcbnew.GetWizardsBackTrace()`, which nothing surfaces. A
plugin that raises during import is indistinguishable from one that is not
installed.

`plugins/__init__.py` is therefore stdlib-only, annotation-free and 3.6-parseable
— it must run on an interpreter it does not support in order to report that.
On any failure it registers a stand-in toolbar button carrying the cause, and
re-raises only when even that fails.

**The invariant: if KiForge is installed, something always appears in the
toolbar** — the plugin, or an explanation. Do not add a code path that can
swallow a load failure silently.

## 7. Studio UI

From KiCad's [UI
policy](https://dev-docs.kicad.org/en/rules-guidelines/ui/index.html):

- **Sizers only.** Absolute positioning is forbidden.
- **Follow the platform theme.** `refresh_palette()` resolves light/dark from
  `wx.SystemAppearance`; never hardcode one ramp. Both ramps must define the
  same keys — a missing key raises inside a paint handler, where exceptions are
  swallowed.
- **Capitalisation**: headers in Header Case, everything else in sentence case.
- **Trailing periods**: none on labels and buttons; complete sentences in errors
  get them.
- **Ellipsis** on any action that opens a dialog (`Browse…`, `Save As…`).
- **Escape must cancel.** Dialogs need a `wxID_CANCEL` path.
- Quote filenames with single quotes.
- Spell strings out; abbreviate only units and universally-known terms (PCB, mm).
- **Dynamic control availability**: disable controls dynamically with explanatory
  tooltips when backing files are missing rather than throwing errors at export
  time. For instance, when exporting from a board file without a matching
  `.kicad_sch` schematic, disable schematic-dependent controls (`chk_sch_pdf`,
  `chk_bom`, `chk_bom_mfr_mpn`). When no `.kicad_pcb` exists or 0 outputs are
  selected, disable `btn_export`.

Custom-painted controls (`_FlatButton`, `_FlatCheckBox`, `_FlatRadioButton`) own
their painting, so wx gives them nothing for free. Two rules learned the hard
way: recompute hover from the pointer's real position rather than trusting
enter/leave events (a captured mouse suppresses `EVT_LEAVE_WINDOW`), and handle
`EVT_MOUSE_CAPTURE_LOST` or wx asserts.

Long work belongs on a worker thread. Never marshal a long render onto the GUI
thread while a dialog is up — it stops timers, freezes repaints, and makes
Cancel do nothing. Cancellation must take effect on the click, not on the next
poll tick.

## 8. Commits

Follow KiCad's [commit message
format](https://dev-docs.kicad.org/en/rules-guidelines/commit/index.html)
loosely, matching this repo's history:

```
fix(scope,scope): imperative one-line summary under ~72 chars

Body explaining what broke and why the fix is shaped this way. Wrap at 72.

BREAKING CHANGE: what downstream users must change.
```

Scopes in use: `studio`, `export`, `cd`, `pcm`, `homebrew`, `compat`, `ci`.

Do not add tool or AI attribution lines to commit messages or PR descriptions.

## 9. Repository layout

| Path | Purpose |
|---|---|
| `kiforge.py` | Core exporter — single source of truth for settings, tasks, CLI |
| `plugins/__init__.py` | Load bootstrap; owns `MIN_PYTHON`. Keep it minimal |
| `plugins/kiforge_studio.py` | wxPython GUI |
| `plugins/kiforge.py` | **Build artifact**, gitignored — see §2 |
| `templates/` | CD workflow templates — edit here only |
| `icons/` | Studio glyphs, bundled into the package |
| `package_plugin.py` | Builds the PCM zip |
| `tests/kicad_runtime_stub.py` | Stand-in `wx`/`pcbnew`; runnable as the load gate |

`ARCHITECTURE.md` §5 covers the KiCad bridging constraints in depth;
`DEVELOPMENT.md` covers the interpreter baseline and day-to-day commands.
