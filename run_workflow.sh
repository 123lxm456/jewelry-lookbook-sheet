#!/usr/bin/env bash
set -euo pipefail

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

workflow_fail() {
  local status="${1:-1}"
  echo "::workflow::error::stage=${WORKFLOW_STAGE}::status=${status}" >&2
  echo "生成失败：阶段「${WORKFLOW_STAGE}」，退出状态 ${status}。请检查输出目录 logs/ 中的请求日志和最近的终端错误。" >&2
  exit "$status"
}

trap workflow_error ERR

OVERALL="${1:-$ROOT_DIR/jewelry-test.jpg}"
DETAIL="${DETAIL_OVERRIDE:-${2:-$OVERALL}}"
STYLE="${STYLE_OVERRIDE:-${3:-$ROOT_DIR/image1.jpg}}"
OUTPUT_DIR="${OUTPUT_OVERRIDE:-${4:-$ROOT_DIR/outputs/imagegen/jewelry-series}}"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
# Use the project-owned adapter by default. A compatible external CLI can
# still be selected explicitly through IMAGE_GEN_CLI.
IMAGE_GEN="${IMAGE_GEN_CLI:-$ROOT_DIR/scripts/image_gen_api.py}"
MAX_ATTEMPTS="${IMAGE2_MAX_ATTEMPTS:-4}"
RETRY_DELAY="${IMAGE2_RETRY_DELAY:-60}"
PARALLELISM="${IMAGE2_PARALLELISM:-5}"
[[ "$PARALLELISM" =~ ^[1-5]$ ]] || { echo "IMAGE2_PARALLELISM must be between 1 and 5" >&2; exit 2; }

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
  analysis_command=("$PYTHON_BIN" "$ROOT_DIR/scripts/analyze_product.py"
    "$OVERALL" "$PRODUCT_SPEC" --env-file "$ENV_FILE")
  if [[ "$FORCE_ANALYSIS" -eq 1 ]]; then
    analysis_command+=(--force)
  fi
  "${analysis_command[@]}"
fi
echo "::workflow::spec_ready"

workflow_stage "商品文案与展示提示词生成"
RUN_FINGERPRINT="$("$PYTHON_BIN" "$ROOT_DIR/scripts/render_product_assets.py" \
  "$PRODUCT_SPEC" "$DETAIL" "$STYLE" "$OUTPUT_DIR")"
echo "::workflow::assets_ready"
COMPLETED_MANIFEST="$OUTPUT_DIR/completed-manifest.json"
COMPLETED_FINGERPRINT=""
if [[ -s "$COMPLETED_MANIFEST" ]]; then
  COMPLETED_FINGERPRINT="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("fingerprint", ""))' "$COMPLETED_MANIFEST")"
fi

if [[ "$DRY_RUN" -eq 0 && "$MIRROR_ONLY" -eq 0 && "$FORCE_ALL" -eq 0 && "$RUN_FINGERPRINT" != "$COMPLETED_FINGERPRINT" ]]; then
  for existing_panel in "$OUTPUT_DIR"/panel-{01,02,03,04,05}.png; do
    if [[ -s "$existing_panel" ]]; then
      echo "Existing panels do not match the current product input." >&2
      echo "Use a new output directory, or pass --force to replace panels in this directory." >&2
      workflow_fail 1
    fi
  done
fi

STYLE_CROP="$OUTPUT_DIR/work/style-reference.jpg"
workflow_stage "图片输入与风格参考预处理"
"$PYTHON_BIN" "$ROOT_DIR/scripts/prepare_style_reference.py" "$STYLE" "$STYLE_CROP"

generate_panel() {
  local number="$1" panel_output="$2" prompt_file="$3"
  local attempt=1 attempt_log command_status
  local command=("$PYTHON_BIN" "$IMAGE_GEN" edit
    --model gpt-image-2
    --image "$OVERALL"
    --image "$DETAIL"
    --image "$STYLE_CROP"
    --prompt-file "$prompt_file"
    --size 1024x1536
    --quality high
    --output-format png
    --out "$panel_output"
    --no-augment)

  if [[ "$DRY_RUN" -eq 1 ]]; then
    command+=(--dry-run)
    "${command[@]}"
    echo "::workflow::panel_ready::$number"
    return
  fi

  workflow_stage "商品展示图片生成：第 ${number} 张"
  while true; do
    attempt_log="$OUTPUT_DIR/logs/panel-$number-attempt-$attempt.log"
    set +e
    "${command[@]}" 2>&1 | tee "$attempt_log"
    command_status="${PIPESTATUS[0]}"
    set -e
    if [[ "$command_status" -eq 0 ]]; then
      echo "::workflow::panel_ready::$number"
      return 0
    fi
    if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]] || \
       ! grep -Eq 'Error code: 502|Bad gateway|retryable.*true|origin_bad_gateway|APIConnectionError|RemoteProtocolError|Connection error|ConnectError|ReadTimeout' "$attempt_log"; then
      echo "Panel $number failed; see $attempt_log" >&2
      workflow_fail "$command_status"
    fi
    echo "Panel $number received a retryable upstream error. Retrying in ${RETRY_DELAY}s ($attempt/$MAX_ATTEMPTS)." >&2
    sleep "$RETRY_DELAY"
    attempt=$((attempt + 1))
  done
}

generation_pids=()
wait_for_generation_batch() {
  local pid failed=0
  for pid in "${generation_pids[@]}"; do
    if ! wait "$pid"; then
      failed=1
    fi
  done
  generation_pids=()
  [[ "$failed" -eq 0 ]]
}

queue_panel() {
  local number="$1" panel_output="$2" prompt_file="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    generate_panel "$number" "$panel_output" "$prompt_file"
    return
  fi
  generate_panel "$number" "$panel_output" "$prompt_file" &
  generation_pids+=("$!")
  if [[ "${#generation_pids[@]}" -ge "$PARALLELISM" ]]; then
    wait_for_generation_batch
  fi
}

for number in 01 02 03 04; do
  if [[ "$MIRROR_ONLY" -eq 1 ]]; then
    [[ -s "$OUTPUT_DIR/panel-$number.png" ]] || { echo "Mirror-only mode requires existing panel-$number.png" >&2; workflow_fail 1; }
    echo "Preserving existing panel: $OUTPUT_DIR/panel-$number.png" >&2
    continue
  fi
  panel_output="$OUTPUT_DIR/panel-$number.png"
  if [[ "$DRY_RUN" -eq 0 && "$FORCE_ALL" -eq 0 && -s "$panel_output" ]]; then
    echo "Skipping completed panel: $panel_output" >&2
    echo "::workflow::panel_ready::$number"
    continue
  fi
  queue_panel "$number" "$panel_output" "$OUTPUT_DIR/prompts/panel-$number.txt" || { status=$?; workflow_fail "$status"; }
done

mirror_output="$OUTPUT_DIR/panel-05.png"
workflow_stage "第 5 张模特镜面展示图生成（image2 单次完整场景）"
if [[ "$DRY_RUN" -eq 1 || "$FORCE_MIRROR" -eq 1 || "$FORCE_ALL" -eq 1 || ! -s "$mirror_output" ]]; then
  queue_panel "05" "$mirror_output" "$OUTPUT_DIR/prompts/panel-05.txt" || { status=$?; workflow_fail "$status"; }
else
  echo "Skipping completed mirror panel: $mirror_output" >&2
  echo "::workflow::panel_ready::05"
fi

wait_for_generation_batch || { status=$?; workflow_fail "$status"; }

# A single text-to-image pass often invents the reflection as a second
# person.  Run a second edit pass against the already generated scene so the
# scene itself remains authoritative and the reflection is constrained by
# mirror geometry, identity and pose correspondence.
if [[ "$DRY_RUN" -eq 0 ]]; then
  workflow_stage "第 5 张镜面几何一致性校正"
  mirror_refined="$OUTPUT_DIR/work/panel-05-mirror-refined.png"
  mirror_refine_log="$OUTPUT_DIR/logs/panel-05-mirror-refine.log"
  mirror_refine_command=("$PYTHON_BIN" "$IMAGE_GEN" edit
    --model gpt-image-2
    --image "$mirror_output"
    --image "$OVERALL"
    --image "$DETAIL"
    --image "$STYLE_CROP"
    --prompt-file "$OUTPUT_DIR/work/panel-05-mirror-refine.txt"
    --size 1024x1536
    --quality high
    --output-format png
    --out "$mirror_refined"
    --no-augment)
  set +e
  "${mirror_refine_command[@]}" 2>&1 | tee "$mirror_refine_log"
  mirror_refine_status="${PIPESTATUS[0]}"
  set -e
  if [[ "$mirror_refine_status" -ne 0 ]]; then
    echo "Mirror geometry correction failed; see $mirror_refine_log" >&2
    workflow_fail "$mirror_refine_status"
  fi
  [[ -s "$mirror_refined" ]] || { echo "Mirror correction produced no image" >&2; workflow_fail 1; }
  mv "$mirror_refined" "$mirror_output"
  echo "::workflow::mirror_refined::05"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  workflow_stage "长图排版与合成（动态避让人物和珠宝）"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/assemble_long_image.py" \
    "$OUTPUT_DIR" "$OUTPUT_DIR/page.json" "$OUTPUT_DIR/jewelry-long.png"
  cp "$OUTPUT_DIR/generation-manifest.json" "$COMPLETED_MANIFEST"
  workflow_stage "文件保存"
  echo "生成成功：最终文件已保存到 $OUTPUT_DIR/jewelry-long.png"
  echo "::workflow::complete"
fi
