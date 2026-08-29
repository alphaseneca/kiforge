#!/bin/bash
# Wrapper for kiforge.py inside Docker and local compose setups.
set -euo pipefail

# Determine the script path (handling action vs local docker compose)
if [ -f "/action/kiforge.py" ]; then
    PYTHON_SCRIPT="/action/kiforge.py"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PYTHON_SCRIPT="${SCRIPT_DIR}/kiforge.py"
fi

# Ensure KiCad's official 3D model library (kicad-packages3d) is present.
#
# The kicad/kicad Docker image deliberately ships without this library (it's
# roughly 10x the base image size), so without this step standard-library
# parts (resistors, capacitors, connectors, ...) would export STEP/3D-render
# output with no 3D body. Always self-healing here rather than opt-in: every
# export should produce complete 3D output by default, with no flag to
# discover or remember to set. The presence check keeps repeat/custom-image
# runs free -- this only ever pays the download cost when the library is
# genuinely missing. Project-bundled 3D models resolved via ${KIPRJMOD}/...
# are unaffected either way.
#
# Shared here (not duplicated in action/run.sh) because both the composite
# Action and local docker-compose runs funnel through this one entrypoint --
# one place owns "does this container have the 3D library", regardless of
# how it was invoked.
_3d_model_dir="${KICAD10_3DMODEL_DIR:-/usr/share/kicad/3dmodels}"
if [ -d "$_3d_model_dir" ] && find "$_3d_model_dir" -maxdepth 1 -iname '*.3dshapes' -print -quit | grep -q .; then
    echo "KiForge: 3D model library already present at ${_3d_model_dir}, skipping download."
elif [ "$(id -u)" -ne 0 ]; then
    echo "KiForge: 3D model library missing and not running as root; cannot install kicad-packages3d. Continuing without it (non-fatal)." >&2
else
    echo "KiForge: downloading KiCad 3D model library (kicad-packages3d) -- this adds real time to this run..."
    if apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends kicad-packages3d -qq; then
        rm -rf /var/lib/apt/lists/*
        echo "KiForge: 3D model library installed."
    else
        echo "KiForge: failed to install kicad-packages3d; continuing export without standard-library 3D bodies (non-fatal)." >&2
    fi
fi

# Execute the Python exporter script directly forwarding all named flags
python3 "$PYTHON_SCRIPT" "$@"
