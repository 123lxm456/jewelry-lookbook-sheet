#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi
DRY_RUN=0
MIRROR_ONLY=0
FORCE_MIRROR=0
FORCE_ALL=0
FORCE_ANALYSIS=0
WORKFLOW_STAGE="启动生成流程"
WORKFLOW_STARTED_SECONDS=$SECONDS
QWEN_ANALYSIS_SECONDS=0
IMAGE_GENERATION_SECONDS=0
SERIES_QUALITY_SECONDS=0
LONG_IMAGE_SECONDS=0
DETAIL_OVERRIDE=""
STYLE_OVERRIDE=""
OUTPUT_OVERRIDE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --mirror-only) MIRROR_ONLY=1 ;;
    --force-mirror) MIRROR_ONLY=1; FORCE_MIRROR=1 ;;
    --force) FORCE_ALL=1; FORCE_MIRROR=1 ;;
    --force-analysis) FORCE_ANALYSIS=1 ;;
    --detail) [[ $# -ge 2 ]] || { echo "--detail requires a path" >&2; exit 2; }; DETAIL_OVERRIDE="$2"; shift ;;
    --style) [[ $# -ge 2 ]] || { echo "--style requires a path" >&2; exit 2; }; STYLE_OVERRIDE="$2"; shift ;;
    --output-dir) [[ $# -ge 2 ]] || { echo "--output-dir requires a path" >&2; exit 2; }; OUTPUT_OVERRIDE="$2"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

workflow_stage() {
  WORKFLOW_STAGE="$1"
  echo "::workflow::stage::$1"
  echo "[阶段] $1"
}

workflow_error() {
  local status=$?
  workflow_fail "$status"
}

cleanup_children() {
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done < <(jobs -pr)
}

workflow_fail() {
  local status="${1:-1}"
  echo "::workflow::error::stage=${WORKFLOW_STAGE}::status=${status}" >&2
  echo "生成失败：阶段「${WORKFLOW_STAGE}」，退出状态 ${status}。请检查输出目录 logs/ 中的请求日志和最近的终端错误。" >&2
  exit "$status"
}

workflow_fail_at() {
  WORKFLOW_STAGE="$1"
  workflow_fail "${2:-1}"
}

cleanup_completed_artifacts() {
  # These inputs are checkpoints only while a job is incomplete.  A completed
  # job can be served, downloaded, and inspected from its six final images,
  # product specification, display plan, and job-state.json.  Keeping them
  # used to retain multiple copies of the style image plus retry prompts,
  # masks, quality reports and request logs for every successful job. Keep
  # prompts: they are the exact model inputs for this job and make a later
  # one-panel repair/audit independent from subsequently edited templates.
  rm -rf "$OUTPUT_DIR/work" "$OUTPUT_DIR/logs"
  rm -f "$OUTPUT_DIR/page.json" "$OUTPUT_DIR/completed-manifest.json" \
    "$OUTPUT_DIR/product-long-layout.json" \
    "$OUTPUT_DIR/jewelry-long.png" "$OUTPUT_DIR/jewelry-long-layout.json"
}

valid_image() {
  "$PYTHON_BIN" -c '
import sys
from PIL import Image
try:
    with Image.open(sys.argv[1]) as image:
        image.verify()
except Exception:
    raise SystemExit(1)
' "$1"
}

trap workflow_error ERR
trap cleanup_children EXIT

OVERALL="${1:-$ROOT_DIR/jewelry-test.jpg}"
DETAIL="${DETAIL_OVERRIDE:-${2:-$OVERALL}}"
STYLE="${STYLE_OVERRIDE:-${3:-$ROOT_DIR/image1.jpg}}"
OUTPUT_DIR="${OUTPUT_OVERRIDE:-${4:-$ROOT_DIR/outputs/imagegen/product-series}}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
# Use the project-owned adapter by default. A compatible external CLI can
# still be selected explicitly through IMAGE_GEN_CLI.
IMAGE_GEN="${IMAGE_GEN_CLI:-$ROOT_DIR/scripts/image_gen_api.py}"
POSTPROCESS_QUALITY_CHECK="${POSTPROCESS_QUALITY_CLI:-$ROOT_DIR/scripts/assess_postprocess_quality.py}"
SERIES_QUALITY_CHECK="${SERIES_QUALITY_CLI:-$ROOT_DIR/scripts/assess_series_quality.py}"
MAX_ATTEMPTS="${IMAGE2_MAX_ATTEMPTS:-4}"
RETRY_DELAY="${IMAGE2_RETRY_DELAY:-60}"
ANALYSIS_MAX_ATTEMPTS="${QWEN_ANALYSIS_ATTEMPTS:-2}"
ANALYSIS_RETRY_DELAY="${QWEN_ANALYSIS_RETRY_DELAY:-5}"
PARALLELISM="${IMAGE2_PARALLELISM:-5}"
[[ "$PARALLELISM" =~ ^[1-5]$ ]] || { echo "IMAGE2_PARALLELISM must be between 1 and 5" >&2; exit 2; }
[[ "$ANALYSIS_MAX_ATTEMPTS" =~ ^[1-8]$ ]] || { echo "QWEN_ANALYSIS_ATTEMPTS must be between 1 and 8" >&2; exit 2; }
[[ "$ANALYSIS_RETRY_DELAY" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "QWEN_ANALYSIS_RETRY_DELAY must be non-negative" >&2; exit 2; }

load_env_key() {
  local wanted="$1" key value
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS='=' read -r key value || [[ -n "$key" ]]; do
    [[ "$key" == "$wanted" ]] || continue
    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    printf '%s' "$value"
    return 0
  done < "$ENV_FILE"
}

ENV_API_KEY="$(load_env_key IMAGE2_API_KEY)"
ENV_BASE_URL="$(load_env_key IMAGE2_BASE_URL)"
ENV_BYPASS_PROXY="$(load_env_key IMAGE2_BYPASS_PROXY)"
PROJECT_API_KEY="${ENV_API_KEY:-${IMAGE2_API_KEY:-}}"
PROJECT_BASE_URL="${ENV_BASE_URL:-${IMAGE2_BASE_URL:-}}"
IMAGE_BYPASS_PROXY="${ENV_BYPASS_PROXY:-${IMAGE2_BYPASS_PROXY:-false}}"
export OPENAI_API_KEY="${PROJECT_API_KEY:-${OPENAI_API_KEY:-}}"
export OPENAI_BASE_URL="${PROJECT_BASE_URL:-${OPENAI_BASE_URL:-}}"

if [[ -n "$OPENAI_BASE_URL" ]]; then
  OPENAI_BASE_URL="${OPENAI_BASE_URL#\"}"
  OPENAI_BASE_URL="${OPENAI_BASE_URL%\"}"
  OPENAI_BASE_URL="${OPENAI_BASE_URL#\'}"
  OPENAI_BASE_URL="${OPENAI_BASE_URL%\'}"
  if [[ "$OPENAI_BASE_URL" != http://* && "$OPENAI_BASE_URL" != https://* ]]; then
    OPENAI_BASE_URL="https://$OPENAI_BASE_URL"
  fi
  export OPENAI_BASE_URL="${OPENAI_BASE_URL%/}"
fi

if [[ -n "$OPENAI_BASE_URL" ]]; then
  echo "Using Image API: $OPENAI_BASE_URL" >&2
fi

case "${IMAGE_BYPASS_PROXY,,}" in
  1|true|yes|on)
    IMAGE_API_HOST="${OPENAI_BASE_URL#*://}"
    IMAGE_API_HOST="${IMAGE_API_HOST%%/*}"
    IMAGE_API_HOST="${IMAGE_API_HOST%%:*}"
    if [[ -n "$IMAGE_API_HOST" ]]; then
      export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$IMAGE_API_HOST"
      export no_proxy="${no_proxy:+$no_proxy,}$IMAGE_API_HOST"
      echo "Bypassing proxy for Image API host: $IMAGE_API_HOST" >&2
    fi
    ;;
  0|false|no|off|"") ;;
  *) echo "IMAGE2_BYPASS_PROXY must be true or false" >&2; exit 2 ;;
esac

# Desktop proxy tools often set both HTTP(S)_PROXY and ALL_PROXY. Prefer the
# HTTP proxy when available so httpx does not require its optional SOCKS stack.
if [[ -n "${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}" ]]; then
  unset ALL_PROXY all_proxy
else
  for proxy_name in ALL_PROXY all_proxy; do
    proxy_value="${!proxy_name:-}"
    if [[ "$proxy_value" == socks://* ]]; then
      printf -v "$proxy_name" '%s' "socks5://${proxy_value#socks://}"
      export "$proxy_name"
    fi
  done
fi

[[ -f "$OVERALL" ]] || { echo "Missing overall reference: $OVERALL" >&2; workflow_fail 1; }
[[ -f "$DETAIL" ]] || { echo "Missing detail reference: $DETAIL" >&2; workflow_fail 1; }
[[ -f "$STYLE" ]] || { echo "Missing style reference: $STYLE" >&2; workflow_fail 1; }
[[ -f "$IMAGE_GEN" ]] || { echo "Missing bundled image CLI: $IMAGE_GEN" >&2; workflow_fail 1; }
if [[ "$DRY_RUN" -eq 0 && -z "$OPENAI_API_KEY" ]]; then
  echo "Set IMAGE2_API_KEY in .env or OPENAI_API_KEY in the environment." >&2
  workflow_fail 1
fi

mkdir -p "$OUTPUT_DIR" "$OUTPUT_DIR/logs" "$OUTPUT_DIR/work"
workflow_stage "图片输入与预处理"
# Style/reference preparation does not depend on visual analysis. Start it now
# so resize, crop and format conversion overlap the Qwen Vision request.
STYLE_CROP="$OUTPUT_DIR/work/style-reference.jpg"
STYLE_PANELS_DIR="$OUTPUT_DIR/work/style-panels"
STYLE_SAFE_PANELS_DIR="$OUTPUT_DIR/work/style-panels-deidentified"
"$PYTHON_BIN" "$ROOT_DIR/scripts/prepare_style_reference.py" "$STYLE" "$STYLE_CROP" \
  --panels-dir "$STYLE_PANELS_DIR" --deidentified-panels-dir "$STYLE_SAFE_PANELS_DIR" &
preprocess_pid=$!
PRODUCT_SPEC="$OUTPUT_DIR/product-spec.json"
if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ -s "$PRODUCT_SPEC" ]]; then
    echo "Using existing product specification for dry run: $PRODUCT_SPEC" >&2
  else
    PRODUCT_SPEC="$ROOT_DIR/product.example.json"
    echo "Using example product specification for network-free dry run." >&2
  fi
else
  workflow_stage "商品信息分析"
  analysis_started_seconds=$SECONDS
  analysis_command=("$PYTHON_BIN" "$ROOT_DIR/scripts/analyze_product.py"
    "$OVERALL" "$PRODUCT_SPEC" --env-file "$ENV_FILE")
  if [[ "$FORCE_ANALYSIS" -eq 1 ]]; then
    analysis_command+=(--force)
  fi
  analysis_status=1
  for analysis_attempt in $(seq 1 "$ANALYSIS_MAX_ATTEMPTS"); do
    set +e
    if [[ "$analysis_attempt" -eq 1 ]]; then
      "${analysis_command[@]}" 2>&1 | tee "$OUTPUT_DIR/logs/analysis.log"
    else
      echo "Retrying Qwen product analysis (${analysis_attempt}/${ANALYSIS_MAX_ATTEMPTS})..." >&2
      "${analysis_command[@]}" 2>&1 | tee -a "$OUTPUT_DIR/logs/analysis.log"
    fi
    analysis_status="${PIPESTATUS[0]}"
    set -e
    [[ "$analysis_status" -eq 0 ]] && break
    # Only retry transient provider/network failures. Configuration, schema,
    # and unsupported-product errors are deterministic and should fail fast.
    if ! grep -Eiq 'timeout|timed out|connection error|connecterror|apiconnectionerror|ratelimit|too many requests|error code: (408|409|429|500|502|503|504)' "$OUTPUT_DIR/logs/analysis.log"; then
      break
    fi
    [[ "$analysis_attempt" -lt "$ANALYSIS_MAX_ATTEMPTS" ]] && sleep "$ANALYSIS_RETRY_DELAY"
  done
  QWEN_ANALYSIS_SECONDS=$((SECONDS - analysis_started_seconds))
  [[ "$analysis_status" -eq 0 ]] || workflow_fail "$analysis_status"
fi
echo "::workflow::spec_ready"
CATEGORY_GROUP="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["identity"]["category_group"])' "$PRODUCT_SPEC")"
"$PYTHON_BIN" -c '
import json, sys
from pathlib import Path
from product_workflow.compatibility import load_product_spec
identity = load_product_spec(Path(sys.argv[1])).identity
print("::workflow::product::" + json.dumps({
    "category_group": identity.category_group,
    "subcategory": identity.subcategory,
    "product_name": identity.product_name,
}, ensure_ascii=False))
' "$PRODUCT_SPEC"

# A product-only authority crop prevents coins, rulers, props, packaging, or
# nearby coordinated products from competing with the actual sale item.
PRODUCT_REFERENCE="$OUTPUT_DIR/work/product-authority-reference.png"
"$PYTHON_BIN" "$ROOT_DIR/scripts/prepare_product_reference.py" \
  "$OVERALL" "$PRODUCT_SPEC" "$PRODUCT_REFERENCE"

workflow_stage "商品文案与展示提示词生成"
PREVIOUS_RUN_FINGERPRINT=""
if [[ -s "$OUTPUT_DIR/generation-manifest.json" ]]; then
  PREVIOUS_RUN_FINGERPRINT="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("fingerprint", ""))' "$OUTPUT_DIR/generation-manifest.json")"
fi
RUN_FINGERPRINT="$("$PYTHON_BIN" "$ROOT_DIR/scripts/render_product_assets.py" \
  "$PRODUCT_SPEC" "$PRODUCT_REFERENCE" "$STYLE" "$OUTPUT_DIR")"
echo "::workflow::assets_ready"
# Copywriting uses only the completed analysis and strategy. It deliberately
# runs beside all image requests and is joined only before final composition.
copy_pid=""
if [[ "$DRY_RUN" -eq 0 && -f "${PRODUCT_SPEC%.json}.copy-pending" ]]; then
  if "$PYTHON_BIN" -c '
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
sections = data.get("copy", data.get("marketing_copy", {})).get("sections", [])
raise SystemExit(0 if any("商品视觉信息正在生成" in str(item.get("body", "")) for item in sections) else 1)
' "$PRODUCT_SPEC"; then
    (set +e
      "$PYTHON_BIN" "$ROOT_DIR/scripts/generate_marketing_copy.py" \
        "$PRODUCT_SPEC" "$OUTPUT_DIR/page.json" --env-file "$ENV_FILE" \
        2>&1 | tee "$OUTPUT_DIR/logs/copy.log"
      exit "${PIPESTATUS[0]}"
    ) &
    copy_pid=$!
  else
    rm -f "${PRODUCT_SPEC%.json}.copy-pending"
    echo "Recovered completed marketing copy from its durable product specification." >&2
  fi
fi
mapfile -t PANEL_NUMBERS < <("$PYTHON_BIN" -c '
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
for panel in plan["panels"]:
    print("%02d" % int(panel["number"]))
' "$OUTPUT_DIR/display-plan.json")
PANEL_TOTAL="${#PANEL_NUMBERS[@]}"
[[ "$PANEL_TOTAL" -gt 0 ]] || { echo "Display plan contains no panels" >&2; workflow_fail 1; }
mapfile -t POSTPROCESS_NUMBERS < <("$PYTHON_BIN" -c '
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
for number in plan.get("postprocessors", {}):
    print(number)
' "$OUTPUT_DIR/display-plan.json")
COMPLETED_MANIFEST="$OUTPUT_DIR/completed-manifest.json"
COMPLETED_FINGERPRINT=""
if [[ -s "$COMPLETED_MANIFEST" ]]; then
  COMPLETED_FINGERPRINT="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("fingerprint", ""))' "$COMPLETED_MANIFEST")"
fi

if [[ "$DRY_RUN" -eq 0 && "$MIRROR_ONLY" -eq 0 && "$FORCE_ALL" -eq 0 && \
      "$RUN_FINGERPRINT" != "$COMPLETED_FINGERPRINT" && "$RUN_FINGERPRINT" != "$PREVIOUS_RUN_FINGERPRINT" ]]; then
  for existing_panel in "$OUTPUT_DIR"/panel-*.png; do
    if [[ -s "$existing_panel" ]]; then
      echo "Existing panels do not match the current product input." >&2
      echo "Use a new output directory, or pass --force to replace panels in this directory." >&2
      workflow_fail 1
    fi
  done
fi

workflow_stage "图片输入与风格参考预处理"
wait "$preprocess_pid" || {
  status=$?
  workflow_fail_at "图片输入与风格参考预处理" "$status"
}

generate_panel() {
  local number="$1" panel_output="$2" prompt_file="$3" style_reference
  local attempt=1 attempt_log command_status
  local command=("$PYTHON_BIN" "$IMAGE_GEN" edit
    --model gpt-image-2
    --image "$PRODUCT_REFERENCE"
  )
  # The uncropped source supplies scale/context but is explicitly subordinate.
  if ! cmp -s "$PRODUCT_REFERENCE" "$OVERALL"; then
    command+=(--image "$OVERALL")
  fi
  if ! cmp -s "$OVERALL" "$DETAIL" && ! cmp -s "$PRODUCT_REFERENCE" "$DETAIL"; then
    command+=(--image "$DETAIL")
  fi
  # Style images often contain another product and can silently override the
  # authority image. Textual per-panel art direction is the safe default.
  # image1 is a jewelry-only visual grammar reference. The de-identified
  # derivative retains pose/composition while suppressing copyable jewelry.
  if [[ "$CATEGORY_GROUP" == "jewelry" && "${IMAGE2_INCLUDE_STYLE_REFERENCE:-true}" =~ ^(1|true|yes|on)$ ]]; then
    style_reference="$STYLE_SAFE_PANELS_DIR/panel-$number.jpg"
    if [[ -s "$style_reference" ]]; then command+=(--image "$style_reference"); fi
  fi
  command+=(--prompt-file "$prompt_file"
    --size 1024x1536
    --quality high
    --output-format png
    --out "$panel_output"
    --no-augment)

  if [[ "$DRY_RUN" -eq 1 ]]; then
    command+=(--dry-run)
    "${command[@]}"
    echo "::workflow::panel_ready::$number::$PANEL_TOTAL"
    return
  fi

  workflow_stage "商品展示图片生成：第 ${number} 张/共 ${PANEL_TOTAL} 张"
  while true; do
    attempt_log="$OUTPUT_DIR/logs/panel-$number-attempt-$attempt.log"
    set +e
    "${command[@]}" 2>&1 | tee "$attempt_log"
    command_status="${PIPESTATUS[0]}"
    set -e
    if [[ "$command_status" -eq 0 ]]; then
      echo "::workflow::panel_ready::$number::$PANEL_TOTAL"
      return 0
    fi
    if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]] || \
       ! grep -Eqi 'Error code: (408|409|429|5[0-9][0-9])|status(_code)?[=: ]+(408|409|429|5[0-9][0-9])|Bad gateway|Too Many Requests|rate.?limit|retryable.*true|origin_bad_gateway|upstream(_error| unavailable)|temporar(il)?y unavailable|APIConnectionError|RemoteProtocolError|Connection error|ConnectError|ReadTimeout|WriteTimeout|PoolTimeout|timed out|timeout' "$attempt_log"; then
      echo "Panel $number failed; see $attempt_log" >&2
      echo "::workflow::panel_error::$number::$attempt_log" >&2
      workflow_fail "$command_status"
    fi
    echo "Panel $number received a retryable upstream error. Retrying in ${RETRY_DELAY}s ($attempt/$MAX_ATTEMPTS)." >&2
    sleep "$RETRY_DELAY"
    attempt=$((attempt + 1))
  done
}

generate_product_lock() {
  local number="$1" mask="$OUTPUT_DIR/work/panel-$1-product-lock-mask.png"
  local source_panel="$OUTPUT_DIR/panel-$1.png"
  local refined_panel="$OUTPUT_DIR/work/panel-$1-product-lock-refined.png"
  local prompt_file="$OUTPUT_DIR/work/panel-$1-quality-retry.txt"
  local log_file="$OUTPUT_DIR/logs/panel-$1-product-lock.log"
  workflow_stage "商品身份局部锁定：第 ${number} 张"
  local command=("$PYTHON_BIN" "$IMAGE_GEN" edit --model gpt-image-2
    --image "$source_panel" --image "$PRODUCT_REFERENCE" --mask "$mask"
    --prompt-file "$prompt_file" --size 1024x1536 --quality high
    --output-format png --out "$refined_panel" --no-augment)
  set +e
  "${command[@]}" 2>&1 | tee "$log_file"
  local status="${PIPESTATUS[0]}"
  set -e
  [[ "$status" -eq 0 ]] || return "$status"
  valid_image "$refined_panel" || return 1
  mv "$refined_panel" "$source_panel"
  rm -f "$mask"
  echo "::workflow::product_lock_ready::$number"
}

prepare_panel_layout() {
  local number="$1"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/prepare_panel_layout.py" \
    "$OUTPUT_DIR/panel-$number.png" "$OUTPUT_DIR/display-plan.json" "$number" \
    "$OUTPUT_DIR/work/layout-cache"
}

generate_postprocess() {
  local number="$1" postprocess_type postprocess_prompt source_panel refined_panel postprocess_log postprocess_status quality_status marker marker_status
  postprocess_type="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["postprocessors"][sys.argv[2]]["type"])' "$OUTPUT_DIR/display-plan.json" "$number")"
  postprocess_prompt="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["postprocessors"][sys.argv[2]]["prompt_file"])' "$OUTPUT_DIR/display-plan.json" "$number")"
  source_panel="$OUTPUT_DIR/panel-$number.png"
  refined_panel="$OUTPUT_DIR/work/panel-$number-${postprocess_type}-refined.png"
  marker="$OUTPUT_DIR/work/panel-$number-$postprocess_type.complete.json"
  marker_status="$("$PYTHON_BIN" -c '
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    print(data.get("status", "") if data.get("fingerprint") == sys.argv[2] else "")
except Exception:
    pass
' "$marker" "$RUN_FINGERPRINT")"
  if [[ "$marker_status" == "running" ]] && valid_image "$refined_panel"; then
    write_postprocess_marker "$marker" complete
    mv "$refined_panel" "$source_panel"
    marker_status="complete"
  fi
  if [[ "$marker_status" == "complete" ]] && valid_image "$source_panel"; then
    echo "Skipping completed postprocess: panel-$number ($postprocess_type)" >&2
    echo "::workflow::postprocess_ready::$number::$postprocess_type"
    prepare_panel_layout "$number"
    return 0
  fi
  workflow_stage "商品展示图片后处理：第 ${number} 张（${postprocess_type}）"
  postprocess_log="$OUTPUT_DIR/logs/panel-$number-${postprocess_type}.log"
  if [[ "$postprocess_type" == "mirror_compose" ]]; then
    refined_panel="$OUTPUT_DIR/work/panel-$number-${postprocess_type}-refined.png"
    write_postprocess_marker "$marker" running
    "$PYTHON_BIN" "$ROOT_DIR/scripts/compose_mirror_scene.py" "$source_panel" "$refined_panel" \
      2>&1 | tee "$postprocess_log"
    valid_image "$refined_panel" || return 1
    write_postprocess_marker "$marker" complete
    mv "$refined_panel" "$source_panel"
    echo "::workflow::postprocess_ready::$number::$postprocess_type"
    prepare_panel_layout "$number"
    return 0
  fi
  quality_status=3
  if [[ "${POSTPROCESS_QUALITY_GATE:-true}" =~ ^(1|true|yes|on)$ ]] && [[ -f "$POSTPROCESS_QUALITY_CHECK" ]] && \
     { [[ -z "${IMAGE_GEN_CLI:-}" ]] || [[ -n "${POSTPROCESS_QUALITY_CLI:-}" ]]; }; then
    set +e
    "$PYTHON_BIN" "$POSTPROCESS_QUALITY_CHECK" --image "$source_panel" --product "$PRODUCT_REFERENCE" \
      --type "$postprocess_type" --env-file "$ENV_FILE" \
      2>&1 | tee "$OUTPUT_DIR/logs/panel-$number-${postprocess_type}-quality.log"
    quality_status="${PIPESTATUS[0]}"
    set -e
  fi
  if [[ "$quality_status" -eq 0 ]]; then
    echo "::workflow::postprocess_skipped::$number::$postprocess_type"
    write_postprocess_marker "$marker" complete
    prepare_panel_layout "$number"
    return 0
  fi
  local postprocess_command=("$PYTHON_BIN" "$IMAGE_GEN" edit --model gpt-image-2 --image "$source_panel" --image "$PRODUCT_REFERENCE")
  if ! cmp -s "$OVERALL" "$DETAIL"; then
    postprocess_command+=(--image "$DETAIL")
  fi
  postprocess_command+=(--image "$STYLE_CROP" --prompt-file "$OUTPUT_DIR/$postprocess_prompt" \
    --size 1024x1536 --quality high --output-format png --out "$refined_panel" --no-augment)
  write_postprocess_marker "$marker" running
  set +e
  "${postprocess_command[@]}" 2>&1 | tee "$postprocess_log"
  postprocess_status="${PIPESTATUS[0]}"
  set -e
  if [[ "$postprocess_status" -ne 0 ]]; then return "$postprocess_status"; fi
  valid_image "$refined_panel" || return 1
  write_postprocess_marker "$marker" complete
  mv "$refined_panel" "$source_panel"
  echo "::workflow::postprocess_ready::$number::$postprocess_type"
  prepare_panel_layout "$number"
}

write_postprocess_marker() {
  "$PYTHON_BIN" -c '
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps({"fingerprint": sys.argv[2], "status": sys.argv[3]}) + "\n", encoding="utf-8")
temporary.replace(path)
' "$1" "$RUN_FINGERPRINT" "$2"
}

clear_postprocess_marker() {
  local number="$1" postprocess_type
  is_postprocess_number "$number" || return 0
  postprocess_type="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["postprocessors"][sys.argv[2]]["type"])' "$OUTPUT_DIR/display-plan.json" "$number")"
  rm -f "$OUTPUT_DIR/work/panel-$number-$postprocess_type.complete.json"
}

is_postprocess_number() {
  local wanted="$1" candidate
  for candidate in "${POSTPROCESS_NUMBERS[@]}"; do
    [[ "$wanted" == "$candidate" ]] && return 0
  done
  return 1
}

generation_pids=()
postprocess_pids=()
layout_pids=()
declare -A PANEL_BY_PID=()

start_postprocess_if_needed() {
  local number="$1"
  if [[ "$DRY_RUN" -eq 0 ]] && is_postprocess_number "$number"; then
    generate_postprocess "$number" &
    postprocess_pids+=("$!")
  fi
}

start_layout_precompute() {
  local number="$1"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    prepare_panel_layout "$number" &
    layout_pids+=("$!")
  fi
}

start_panel() {
  local number="$1"
  generate_panel "$number" "$OUTPUT_DIR/panel-$number.png" "$OUTPUT_DIR/prompts/panel-$number.txt" &
  local pid=$!
  generation_pids+=("$pid")
  PANEL_BY_PID["$pid"]="$number"
}

wait_for_one_panel() {
  local finished_pid="" status=0 number pid remaining=()
  set +e
  wait -n -p finished_pid "${generation_pids[@]}"
  status=$?
  set -e
  if [[ "$status" -ne 0 ]]; then
    number="${PANEL_BY_PID[$finished_pid]:-未知}"
    if [[ "$number" == "未知" ]]; then
      workflow_fail "$status"
    fi
    workflow_fail_at "商品展示图片生成：第 ${number} 张/共 ${PANEL_TOTAL} 张" "$status"
  fi
  number="${PANEL_BY_PID[$finished_pid]}"
  unset 'PANEL_BY_PID[$finished_pid]'
  for pid in "${generation_pids[@]}"; do
    [[ "$pid" != "$finished_pid" ]] && remaining+=("$pid")
  done
  generation_pids=("${remaining[@]}")
  # A mirror repair starts the moment its source panel is ready; unrelated
  # panel workers continue occupying and refilling the rolling pool.
  if is_postprocess_number "$number"; then
    start_postprocess_if_needed "$number"
  else
    start_layout_precompute "$number"
  fi
}

image_generation_started_seconds=$SECONDS
if [[ "$MIRROR_ONLY" -eq 1 && "${#POSTPROCESS_NUMBERS[@]}" -eq 0 ]]; then
  echo "The selected product strategy has no mirror/postprocess panel." >&2
  workflow_fail 2
fi

for number in "${PANEL_NUMBERS[@]}"; do
  is_postprocess_panel=0
  is_postprocess_number "$number" && is_postprocess_panel=1
  if [[ "$MIRROR_ONLY" -eq 1 && "$is_postprocess_panel" -eq 0 ]]; then
    [[ -s "$OUTPUT_DIR/panel-$number.png" ]] || { echo "Mirror-only mode requires existing panel-$number.png" >&2; workflow_fail 1; }
    echo "Preserving existing panel: $OUTPUT_DIR/panel-$number.png" >&2
    continue
  fi
  panel_output="$OUTPUT_DIR/panel-$number.png"
  if [[ "$DRY_RUN" -eq 0 && "$FORCE_ALL" -eq 0 && "$FORCE_MIRROR" -eq 0 && -s "$panel_output" ]] && valid_image "$panel_output"; then
    echo "Skipping completed panel: $panel_output" >&2
    echo "::workflow::panel_ready::$number::$PANEL_TOTAL"
    if is_postprocess_number "$number"; then
      start_postprocess_if_needed "$number"
    else
      start_layout_precompute "$number"
    fi
    continue
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    generate_panel "$number" "$panel_output" "$OUTPUT_DIR/prompts/panel-$number.txt"
  else
    clear_postprocess_marker "$number"
    while [[ "${#generation_pids[@]}" -ge "$PARALLELISM" ]]; do
      wait_for_one_panel
    done
    start_panel "$number"
  fi
done

while [[ "${#generation_pids[@]}" -gt 0 ]]; do wait_for_one_panel; done
for pid in "${postprocess_pids[@]}"; do wait "$pid" || { status=$?; workflow_fail "$status"; }; done
for pid in "${layout_pids[@]}"; do wait "$pid" || { status=$?; workflow_fail "$status"; }; done
IMAGE_GENERATION_SECONDS=$((SECONDS - image_generation_started_seconds))
if [[ -n "$copy_pid" ]]; then wait "$copy_pid" || { status=$?; workflow_fail "$status"; }; fi

if [[ "$DRY_RUN" -eq 0 && "${SERIES_QUALITY_GATE:-true}" =~ ^(1|true|yes|on)$ ]] && \
   [[ -f "$SERIES_QUALITY_CHECK" ]] && \
   { [[ -z "${IMAGE_GEN_CLI:-}" ]] || [[ -n "${SERIES_QUALITY_CLI:-}" ]]; }; then
  semantic_attempt=1
  semantic_max_attempts="${IMAGE2_SEMANTIC_MAX_ATTEMPTS:-2}"
  [[ "$semantic_max_attempts" =~ ^[1-3]$ ]] || { echo "IMAGE2_SEMANTIC_MAX_ATTEMPTS must be between 1 and 3" >&2; workflow_fail 2; }
  while true; do
    workflow_stage "五图商品一致性与重复度校验：第 ${semantic_attempt} 次"
    rm -f "$OUTPUT_DIR/work"/panel-??-product-lock-mask.png
    quality_call_started_seconds=$SECONDS
    # A status of 3 is a normal control signal meaning "regenerate the listed
    # panels", not a workflow failure. Keep the pipeline in an if-condition so
    # Bash does not dispatch the global ERR trap before we can inspect it.
    if "$PYTHON_BIN" "$SERIES_QUALITY_CHECK" --output-dir "$OUTPUT_DIR" \
      --product "$PRODUCT_REFERENCE" --spec "$PRODUCT_SPEC" --display-plan "$OUTPUT_DIR/display-plan.json" \
      --attempt "$semantic_attempt" --env-file "$ENV_FILE" \
      2>&1 | tee "$OUTPUT_DIR/logs/series-quality-attempt-$semantic_attempt.log"; then
      quality_status=0
    else
      quality_pipeline_status=("${PIPESTATUS[@]}")
      quality_status="${quality_pipeline_status[0]}"
    fi
    SERIES_QUALITY_SECONDS=$((SERIES_QUALITY_SECONDS + SECONDS - quality_call_started_seconds))
    [[ "$quality_status" -eq 0 ]] && break
    # The required product analysis and per-panel prompts remain authoritative;
    # a best-effort series gate must not hold completed images indefinitely.
    if [[ "$quality_status" -eq 2 ]]; then
      echo "Series vision gate unavailable within its time budget; keeping prompt-constrained panels after local duplicate screening." >&2
      echo "::workflow::quality_degraded::series-vision-unavailable"
      break
    fi
    [[ "$quality_status" -eq 3 ]] || workflow_fail "$quality_status"
    if [[ "$semantic_attempt" -ge "$semantic_max_attempts" ]]; then
      echo "Generated series still fails product fidelity or diversity thresholds after $semantic_attempt assessment(s)." >&2
      workflow_fail 3
    fi
    mapfile -t QUALITY_RETRY_PANELS < "$OUTPUT_DIR/work/series-quality.retry"
    [[ "${#QUALITY_RETRY_PANELS[@]}" -gt 0 ]] || workflow_fail 3
    for number in "${QUALITY_RETRY_PANELS[@]}"; do
      [[ "$number" =~ ^0[1-5]$ ]] || workflow_fail 3
      workflow_stage "质量校验自动重生成：第 ${number} 张"
      clear_postprocess_marker "$number"
      retry_generation_started_seconds=$SECONDS
      if [[ -s "$OUTPUT_DIR/work/panel-$number-product-lock-mask.png" ]]; then
        generate_product_lock "$number"
      else
        generate_panel "$number" "$OUTPUT_DIR/panel-$number.png" "$OUTPUT_DIR/work/panel-$number-quality-retry.txt"
        if is_postprocess_number "$number"; then
          generate_postprocess "$number"
        fi
      fi
      IMAGE_GENERATION_SECONDS=$((IMAGE_GENERATION_SECONDS + SECONDS - retry_generation_started_seconds))
    done
    semantic_attempt=$((semantic_attempt + 1))
  done
fi

# Quality retries may replace panels after speculative layout preparation.
# Rebuild every cache entry from the accepted final pixels.
if [[ "$DRY_RUN" -eq 0 ]]; then
  for number in "${PANEL_NUMBERS[@]}"; do prepare_panel_layout "$number"; done
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  workflow_stage "长图排版与合成（动态避让商品与人物）"
  long_image_started_seconds=$SECONDS
  "$PYTHON_BIN" "$ROOT_DIR/scripts/assemble_long_image.py" \
    "$OUTPUT_DIR" "$OUTPUT_DIR/page.json" "$OUTPUT_DIR/product-long.png" \
    --display-plan "$OUTPUT_DIR/display-plan.json" --prepared-dir "$OUTPUT_DIR/work/layout-cache"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/package_product_images.py" "$OUTPUT_DIR"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/sync_job_state.py" "$OUTPUT_DIR"
  LONG_IMAGE_SECONDS=$((SECONDS - long_image_started_seconds))
  rm -f "$OUTPUT_DIR/work/layout-cache"/panel-*.ppm "$OUTPUT_DIR/work/layout-cache"/risk-*.png
  QWEN_RETRY_COUNT="$($PYTHON_BIN -c '
import re, sys
from pathlib import Path
print(sum(len(re.findall(r"Qwen (?:request|model discovery) failed transiently", path.read_text(encoding="utf-8", errors="ignore"))) for path in Path(sys.argv[1]).glob("*.log")))
' "$OUTPUT_DIR/logs")"
  DUPLICATE_SECONDS="$($PYTHON_BIN -c '
import json, sys
from pathlib import Path
total = 0.0
for path in Path(sys.argv[1]).glob("series-quality-attempt-*.json"):
    try:
        total += float(json.loads(path.read_text(encoding="utf-8")).get("timing", {}).get("local_duplicate_seconds", 0))
    except (OSError, ValueError):
        pass
print(f"{total:.3f}")
' "$OUTPUT_DIR/work")"
  TOTAL_SECONDS=$((SECONDS - WORKFLOW_STARTED_SECONDS))
  "$PYTHON_BIN" -c '
import json, sys
from pathlib import Path
labels = ("qwen_analysis_seconds", "qwen_retry_count", "series_consistency_seconds", "duplicate_check_seconds", "image_generation_seconds", "long_image_seconds", "total_seconds")
values = [float(value) for value in sys.argv[2:]]
data = dict(zip(labels, values))
data["qwen_retry_count"] = int(data["qwen_retry_count"])
path = Path(sys.argv[1])
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
' "$OUTPUT_DIR/logs/timing.json" "$QWEN_ANALYSIS_SECONDS" "$QWEN_RETRY_COUNT" "$SERIES_QUALITY_SECONDS" "$DUPLICATE_SECONDS" "$IMAGE_GENERATION_SECONDS" "$LONG_IMAGE_SECONDS" "$TOTAL_SECONDS"
  echo "Qwen分析耗时：${QWEN_ANALYSIS_SECONDS}s"
  echo "Qwen重试次数：${QWEN_RETRY_COUNT}"
  echo "五图一致性检测耗时：${SERIES_QUALITY_SECONDS}s"
  echo "重复度检测耗时：${DUPLICATE_SECONDS}s"
  echo "图片生成耗时：${IMAGE_GENERATION_SECONDS}s"
  echo "长图生成耗时：${LONG_IMAGE_SECONDS}s"
  echo "总耗时：${TOTAL_SECONDS}s"
  workflow_stage "文件保存"
  cleanup_completed_artifacts
  echo "生成成功：最终文件已保存到 $OUTPUT_DIR/product-long.png"
  echo "::workflow::complete"
fi
