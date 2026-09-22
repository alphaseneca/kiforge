# Python Interpreter Resolution & Subprocess Invariants

## Invariant
KiForge runs in diverse Python environments: inside KiCad's bundled Python (Studio), under system Python (CLI), and in headless CI containers. Shipped code must observe the Python 3.9 floor and execution boundaries.

## Rules

### 1. `sys.executable` Ambiguity in GUI
- Inside KiCad GUI (Studio), `sys.executable` is `kicad.exe` or `KiCad.app`, **not Python**.
- Any subprocess or dependency probe targeting KiCad's Python environment must resolve the binary via `PathResolver.get_kicad_python_path()`.

### 2. String Path Validation for Subprocess Python
- In `_export_pdf_via_subprocess` or subprocess runners, always validate `isinstance(python_exe, str)` before checking file existence:
  ```python
  exe = (python_exe if isinstance(python_exe, str) else None) or sys.executable
  ```
- *Rationale*: In unit tests, `MagicMock` contexts auto-create `__fspath__`, making `isinstance(mock, os.PathLike)` evaluate to `True`, which causes a `TypeError` when passed to `os.path.isfile()`.

### 3. Python 3.9 Floor
- All shipped modules must carry `from __future__ import annotations`.
- Do not use syntax or stdlib APIs newer than 3.9 (`match`, `tomllib`, `zip(strict=)`, `X | Y` at runtime).
- Keep `plugins/__init__.py:MIN_PYTHON`, `pyproject.toml`, `[tool.ruff] target-version`, and CI matrix in sync.

### 4. Build Artifact Shadowing Guard (`plugins/kiforge.py`)
- `package_plugin.py` copies root `kiforge.py` into `plugins/kiforge.py` for packaging.
- If `plugins/kiforge.py` remains in the workspace, `plugins/kiforge_studio.py` binds to that stale copy instead of the active `kiforge.py`.
- Always delete `plugins/kiforge.py` immediately after running packaging or tests.
