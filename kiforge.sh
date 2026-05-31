#!/bin/bash
set -e

# Determine the script path (handling action vs local docker compose)
if [ -f "/action/kiforge.py" ]; then
    PYTHON_SCRIPT="/action/kiforge.py"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PYTHON_SCRIPT="${SCRIPT_DIR}/kiforge.py"
fi

# Execute the Python exporter script directly forwarding all named flags
python3 "$PYTHON_SCRIPT" "$@"
