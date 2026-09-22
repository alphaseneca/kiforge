# Descriptive Error & Warning Logging Standards

## Invariant
Never emit vague summaries like `"Export completed with 4 warning(s)"` or generic `"Command failed"` notices without immediately following with an itemized, numbered breakdown of the exact failures and component contexts.

## Logging Requirements

### 1. Itemized Warning Summaries
Whenever warnings or non-fatal step failures occur, `kiforge.log` must record an explicit, numbered list detailing each specific warning:
```python
if self.context.warnings:
    self.context.logger.warning(
        "KiForge export completed with %d warning(s):", len(self.context.warnings)
    )
    for idx, w in enumerate(self.context.warnings, 1):
        self.context.logger.warning("  [%d/%d] %s", idx, len(self.context.warnings), w)
```

### 2. Full Subprocess Diagnostic Context
When any CLI subprocess (`kicad-cli`, Python renderers, etc.) fails:
- Log at `ERROR` level: the task name, subprocess exit code, the full command line arguments, the execution working directory, and the complete `stderr`/`stdout` text.
- Never swallow or bury process error output solely in `DEBUG` level when a step has failed.

### 3. Component Designator Attribution
When converting, resolving, or warning about 3D models or footprint assets:
- Map model paths back to their component reference designators (e.g. `[U3]`, `[D1]`, or `[LED1..LED26]`).
- Include the component designators in logs so the user immediately knows which footprints on the board are affected.
