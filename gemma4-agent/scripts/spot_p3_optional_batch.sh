#!/usr/bin/env bash
# Optional CPU follow-up after spot_p3_metrics_batch.sh
set -euo pipefail

BUCKET="${GEMMA4_S3_BUCKET:-gemma4-dev-agent-data-650825122703}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
WORK="${GEMMA4_WORK_DIR:-/tmp/gemma4-p3-metrics}"
SCRIPTS="$WORK/gemma4-agent/scripts"
DATA="$WORK/data"
OUT="$WORK/results"

mkdir -p "$OUT" "$WORK/traces" "$WORK/gemma4-agent/agent"
cd "$WORK"

aws s3 sync "s3://${BUCKET}/tools/gemma4-agent-scripts/" "$SCRIPTS/" --region "$REGION"
aws s3 sync "s3://${BUCKET}/results/trace_exports/" "$WORK/traces/" --region "$REGION"
aws s3 sync "s3://${BUCKET}/tools/gemma4-agent-agent/" "$WORK/gemma4-agent/agent/" --region "$REGION"

echo "[optional] data inventory..."
python3 "$SCRIPTS/inventory_data.py" --data "$DATA" --out "$OUT/local_inventory.json"

echo "[optional] trace KPIs..."
python3 "$SCRIPTS/compute_trace_kpis.py" --traces-root "$WORK/traces" --out "$OUT/trace_kpis_all.json"

echo "[optional] classify any new trace dirs..."
for d in "$WORK/traces"/*/; do
  [[ -d "$d" ]] || continue
  name="$(basename "$d")"
  out="$OUT/${name}_classified.json"
  if [[ ! -f "$out" ]] || [[ "$d" -nt "$out" ]]; then
    python3 "$SCRIPTS/classify_failures.py" --traces "$d" --out "$out"
  fi
done

if [[ -f "$WORK/gemma4-agent/agent/agent.yaml" ]]; then
  echo "[optional] submission zip dry-run..."
  python3 "$SCRIPTS/build_submission.py" \
    --agent-dir "$WORK/gemma4-agent/agent" \
    --out "$OUT/submission_dryrun.zip"
  python3 - <<'PY'
import json
from pathlib import Path
p = Path("/tmp/gemma4-p3-metrics/results/submission_dryrun.zip")
import hashlib
h = hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps({"zip_bytes": p.stat().st_size, "sha256": h}))
PY
else
  echo "[optional] skip zip: agent/ not on S3 (upload tools/gemma4-agent-agent/)"
fi

aws s3 sync "$OUT/" "s3://${BUCKET}/results/p3_metrics_batch/" --region "$REGION"
echo "[optional] done → s3://${BUCKET}/results/p3_metrics_batch/"
