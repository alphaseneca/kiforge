#!/bin/bash
set -e

# Arguments
PROJECT_PATH="${1:-.}"
OUTPUT_DIR="${2:-kiforge}"
EXPORT_3D="${3:-true}"
EXPORT_SVG="${4:-true}"
EXPORT_BOM="${5:-true}"
EXPORT_SCH_PDF="${6:-true}"
EXPORT_POS="${7:-true}"
EXPORT_STEP="${8:-true}"
EXPORT_GERBERS="${9:-true}"
EXPORT_DRILLS="${10:-true}"

# Determine the script path (handling action vs local docker compose)
if [ -f "/action/kiforge.py" ]; then
    PYTHON_SCRIPT="/action/kiforge.py"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PYTHON_SCRIPT="${SCRIPT_DIR}/kiforge.py"
fi

# Execute the Python exporter script (which is the single source of truth)
python3 "$PYTHON_SCRIPT" \
    "$PROJECT_PATH" \
    "$OUTPUT_DIR" \
    "$EXPORT_3D" \
    "$EXPORT_SVG" \
    "$EXPORT_BOM" \
    "$EXPORT_SCH_PDF" \
    "$EXPORT_POS" \
    "$EXPORT_STEP" \
    "$EXPORT_GERBERS" \
    "$EXPORT_DRILLS"
