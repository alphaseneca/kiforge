#!/usr/bin/env bash
# KiForge composite GitHub Action runner.
#
# Builds the KiForge Docker image, runs kiforge.py inside kicad/kicad:10.0, and
# restores workspace ownership on the output folder. Invoked from action.yml;
# inputs are passed via INPUT_* environment variables.
set -euo pipefail

action_path="${KIFORGE_ACTION_PATH:?KIFORGE_ACTION_PATH is required}"
workspace="${KIFORGE_WORKSPACE:?KIFORGE_WORKSPACE is required}"
project_path="${INPUT_PROJECT_PATH:-.}"
output_dir="${INPUT_OUTPUT_DIR:-kiforge}"

build_cli_args() {
  local -a args=()
  args+=("--project-path" "$project_path")
  args+=("--output-dir" "$output_dir")

  [[ "${INPUT_EXPORT_3D:-true}" != "false" ]] || args+=("--no-export-3d")
  [[ "${INPUT_EXPORT_SVG:-true}" != "false" ]] || args+=("--no-export-svg")
  [[ "${INPUT_EXPORT_BOM:-true}" != "false" ]] || args+=("--no-export-bom")
  [[ "${INPUT_EXPORT_SCH_PDF:-true}" != "false" ]] || args+=("--no-export-sch-pdf")
  [[ "${INPUT_EXPORT_POS:-true}" != "false" ]] || args+=("--no-export-pos")
  [[ "${INPUT_EXPORT_STEP:-true}" != "false" ]] || args+=("--no-export-step")
  [[ "${INPUT_EXPORT_GERBERS:-true}" != "false" ]] || args+=("--no-export-gerbers")
  [[ "${INPUT_EXPORT_DRILLS:-true}" != "false" ]] || args+=("--no-export-drills")
  [[ "${INPUT_EXPORT_IBOM:-true}" != "false" ]] || args+=("--no-export-ibom")
  [[ "${INPUT_FORMAT_JLC:-true}" != "false" ]] || args+=("--no-format-jlc")
  [[ "${INPUT_SYNC_TITLE_BLOCK_REV:-true}" != "false" ]] || args+=("--no-sync-title-block-rev")
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
