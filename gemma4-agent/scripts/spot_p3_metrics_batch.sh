#!/usr/bin/env bash
# P3 metrics batch — CPU worker (EC2 Spot / on-demand). No GPU / no vLLM.
set -euo pipefail

BUCKET="${GEMMA4_S3_BUCKET:-gemma4-dev-agent-data-650825122703}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
WORK="${GEMMA4_WORK_DIR:-/tmp/gemma4-p3-metrics}"
REPO_URL="${GEMMA4_REPO_URL:-}"

mkdir -p "$WORK/results"
cd "$WORK"

echo "[batch] sync competition data (incremental)..."
aws s3 sync "s3://${BUCKET}/competition/full/" "$WORK/data/" --region "$REGION" || true

if [[ -n "$REPO_URL" ]]; then
  if [[ ! -d gemma4-agent/.git ]]; then
    git clone --depth 1 "$REPO_URL" gemma4-agent
  fi
else
  aws s3 sync "s3://${BUCKET}/tools/gemma4-agent-scripts/" "$WORK/gemma4-agent/scripts/" --region "$REGION"
  mkdir -p "$WORK/gemma4-agent/configs"
  aws s3 cp "s3://${BUCKET}/tools/gemma4-agent-configs/splits.json" \
    "$WORK/gemma4-agent/configs/splits.json" --region "$REGION" || true
fi

ROOT="$WORK/gemma4-agent"
DATA="$WORK/data"
if [[ ! -f "$DATA/tasks.jsonl" ]]; then
  echo "[batch] ERROR: missing tasks.jsonl under $DATA" >&2
  exit 1
fi

run_py() {
  python3 "$@"
}

echo "[batch] loc Hit@5 smoke + dev..."
run_py "$ROOT/scripts/measure_loc_hitk.py" --data "$DATA" --split "$ROOT/configs/splits.json:smoke" \
  --out "$WORK/results/loc_hitk_smoke.json"
run_py "$ROOT/scripts/measure_loc_hitk.py" --data "$DATA" --split "$ROOT/configs/splits.json:dev" \
  --out "$WORK/results/loc_hitk_dev.json"

if aws s3 ls "s3://${BUCKET}/results/trace_exports/" --region "$REGION" >/dev/null 2>&1; then
  echo "[batch] sync trace exports + classify..."
  aws s3 sync "s3://${BUCKET}/results/trace_exports/" "$WORK/traces/" --region "$REGION"
  for d in "$WORK/traces"/*/; do
    [[ -d "$d" ]] || continue
    name="$(basename "$d")"
    run_py "$ROOT/scripts/classify_failures.py" --traces "$d" --out "$WORK/results/${name}_classified.json"
  done
fi

echo "[batch] push results..."
aws s3 sync "$WORK/results/" "s3://${BUCKET}/results/p3_metrics_batch/" --region "$REGION"
echo "[batch] done → s3://${BUCKET}/results/p3_metrics_batch/"
