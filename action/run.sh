#!/usr/bin/env bash
# KiForge composite GitHub Action runner.
#
# Maps action.yml INPUT_* variables to the same CLI flags as `python kiforge.py`.
# Configurable: EXPORT_SETTING_KEYS toggles + EXPORT_PARAM_SPECS + RUNTIME_OPTION_SPECS.
# Not passed through: BOM_EXPORT_DEFAULTS, RENDER_3D_DEFAULTS (hardcoded in kiforge.py).
#
# Builds the KiForge Docker image, runs kiforge.py inside kicad/kicad:10.0, and
# restores workspace ownership on the output folder.
set -euo pipefail

action_path="${KIFORGE_ACTION_PATH:?KIFORGE_ACTION_PATH is required}"
workspace="${KIFORGE_WORKSPACE:?KIFORGE_WORKSPACE is required}"
project_path="${INPUT_PROJECT_PATH:-.}"
output_dir="${INPUT_OUTPUT_DIR:-kiforge}"

append_bool_toggle() {
  local env_name="$1"
  local flag="$2"
  local value="${!env_name:-true}"
  if [[ "$value" == "false" ]]; then
    args+=("--no-${flag}")
  fi
}

build_cli_args() {
  local -a args=()
  args+=("--project-path" "$project_path")
  args+=("--output-dir" "$output_dir")

  # Export toggles (EXPORT_SETTING_KEYS)
  append_bool_toggle INPUT_EXPORT_3D export-3d
  append_bool_toggle INPUT_EXPORT_SVG export-svg
  append_bool_toggle INPUT_EXPORT_BOM export-bom
  append_bool_toggle INPUT_EXPORT_SCH_PDF export-sch-pdf
  append_bool_toggle INPUT_EXPORT_POS export-pos
  append_bool_toggle INPUT_EXPORT_STEP export-step
  append_bool_toggle INPUT_EXPORT_GERBERS export-gerbers
  append_bool_toggle INPUT_EXPORT_DRILLS export-drills
  append_bool_toggle INPUT_EXPORT_IBOM export-ibom
  append_bool_toggle INPUT_FORMAT_JLC format-jlc

  # Export parameters (EXPORT_PARAM_SPECS)
  if [[ -n "${INPUT_POS_SIDE:-}" ]]; then
    args+=("--pos-side" "${INPUT_POS_SIDE}")
  fi
  append_bool_toggle INPUT_POS_SMD_ONLY pos-smd-only
  append_bool_toggle INPUT_POS_EXCLUDE_DNP pos-exclude-dnp
  append_bool_toggle INPUT_STEP_SUBST_MODELS step-subst-models
  append_bool_toggle INPUT_BOM_INCLUDE_MFR_MPN bom-include-mfr-mpn

  # Runtime options (RUNTIME_OPTION_SPECS)
  append_bool_toggle INPUT_SYNC_TITLE_BLOCK_REV sync-title-block-rev

  if [[ -n "${INPUT_VERSION:-}" ]]; then
    args+=("--version-tag" "${INPUT_VERSION}")
  fi

  printf '%s\0' "${args[@]}"
}

detect_runner_container_id() {
  local cid=""

  if [[ -n "$(hostname)" ]] && docker inspect "$(hostname)" &>/dev/null; then
    echo "$(hostname)"
    return 0
  fi

  if [[ -f /proc/self/cgroup ]]; then
    cid="$(grep -o -E '[0-9a-f]{64}' /proc/self/cgroup 2>/dev/null | head -n 1 || true)"
    if [[ -n "$cid" ]] && docker inspect "$cid" &>/dev/null; then
      echo "$cid"
      return 0
    fi
  fi

  if [[ -f /proc/self/mountinfo ]]; then
    cid="$(grep -o -E '[0-9a-f]{64}' /proc/self/mountinfo 2>/dev/null | head -n 1 || true)"
    if [[ -n "$cid" ]] && docker inspect "$cid" &>/dev/null; then
      echo "$cid"
      return 0
    fi
  fi

  return 1
}

mapfile -d '' -t cli_args < <(build_cli_args)

echo "Building KiForge Docker image..."
docker build -t kiforge:latest "$action_path"

echo "Running KiForge exporter..."
docker_env=(
  -e GITHUB_ACTIONS=true
  -e GITHUB_REF_NAME="${GITHUB_REF_NAME:-}"
  -e GITHUB_REF_TYPE="${GITHUB_REF_TYPE:-}"
  -e VERSION="${VERSION:-}"
  -e KICAD10_3DMODEL_DIR="${KICAD10_3DMODEL_DIR:-}"
  -e KISYS3DMOD="${KISYS3DMOD:-}"
  -e KIPRJMOD="${KIPRJMOD:-}"
)

if container_id="$(detect_runner_container_id)"; then
  echo "Detected nested Docker runner ($container_id); using --volumes-from."
  docker run --rm \
    --user root \
    "${docker_env[@]}" \
    --volumes-from "$container_id" \
    -w "$workspace" \
    kiforge:latest \
    /bin/bash /action/kiforge.sh "${cli_args[@]}"
else
  echo "Running on host; mounting workspace at /workspace."
  docker run --rm \
    --user root \
    "${docker_env[@]}" \
    -v "${workspace}:/workspace" \
    -w /workspace \
    kiforge:latest \
    /bin/bash /action/kiforge.sh "${cli_args[@]}"
fi

output_path="${project_path%/}/${output_dir}"
if [[ -d "$output_path" ]]; then
  echo "Restoring ownership on ${output_path}..."
  sudo chown -R "$(id -u):$(id -g)" "$output_path"
fi
