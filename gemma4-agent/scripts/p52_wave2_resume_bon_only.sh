#!/bin/bash
# P5.2c — Resume Stage-1 BoN only after pod loss. Does NOT wipe traces/bon.
# Assumes bootstrap already built venv + model + agent + gold artifacts (or restored from S3).
# rejection_sample.py resumes per-sample via existing raw/*/n*/meta.json.
set -uo pipefail
exec >> /workspace/p52_wave2_pipeline.log 2>&1

if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
export VLLM_USE_FLASHINFER_SAMPLER=0
export PIP_DISABLE_PIP_VERSION_CHECK=1

BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p51
STATUS_FILE=/workspace/p52_wave2.status
VENV_PY="$ROOT/.venv_bootstrap/bin/python"
MODEL_PATH=/models/gemma-4-31B-it-qat-w4a16-ct

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

persist_p52() {
  local rc=0
  aws s3 sync "$ROOT/traces/bon/" "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || rc=1
  aws s3 sync "$ROOT/results/p52_wave2_sieve/" "s3://$BUCKET/results/p52_wave2_sieve/" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_pipeline.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_pipeline.log" --only-show-errors || true
  return "$rc"
}

echo "=== P5.2 RESUME BON-ONLY $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
cd "$ROOT" || { set_status RESUME_FAIL_NO_ROOT; exit 2; }
[ -x "$VENV_PY" ] || { set_status RESUME_FAIL_NO_VENV; exit 3; }
[ -d "$MODEL_PATH" ] || { set_status RESUME_FAIL_NO_MODEL; exit 4; }

export P31_ROOT="$ROOT" P5_ROOT="$ROOT"
export MODEL_PATH BON_TEMPERATURE=0.75 SWE_SANDBOX=subprocess
export BON_CONCURRENCY=2 BON_COMMAND_TIMEOUT=75

# Restore gold + live list + prior BoN traces (never wipe)
mkdir -p "$ROOT/results/p52_wave2_sieve" "$ROOT/configs" traces/bon traces/star
aws s3 cp "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_wave2_a40.json" \
  "$ROOT/results/p52_wave2_sieve/gold_gate_wave2_a40.json" --only-show-errors || true
aws s3 cp "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_live_filter.json" \
  "$ROOT/results/p52_wave2_sieve/gold_gate_live_filter.json" --only-show-errors || true
if [ -f /workspace/p52_wave2_bon_live.txt ]; then
  cp -f /workspace/p52_wave2_bon_live.txt "$ROOT/configs/p52_wave2_bon_live.txt"
fi
if [ ! -f "$ROOT/configs/p52_wave2_bon_live.txt" ]; then
  # Rebuild live from gold ∩ requests-first (G4)
  "$VENV_PY" - <<'PY'
import json
from pathlib import Path
root = Path("/workspace/gemma4-p51")
sp = json.loads((root / "configs" / "splits.json").read_text())
ban = set(sp.get("holdout") or []) | set(sp.get("smoke") or [])
rep = json.loads((root / "results/p52_wave2_sieve/gold_gate_wave2_a40.json").read_text())
ordered = [ln.strip() for ln in (root / "configs" / "p52_wave2_tasks_requests_first.txt").read_text().splitlines() if ln.strip()]
pass_ids = {r["task_id"] for r in rep.get("results") or [] if r.get("resolved")}
live = [t for t in ordered if t in pass_ids and t not in ban]
(root / "configs" / "p52_wave2_bon_live.txt").write_text("\n".join(live) + ("\n" if live else ""))
print("REBUILT_LIVE", len(live))
if not live:
    raise SystemExit(16)
PY
  [ $? -eq 0 ] || { set_status GOLD_GATE_EMPTY; exit 16; }
fi

echo "=== SYNC BON FROM S3 (preserve) $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
aws s3 sync "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" traces/bon/ --only-show-errors || true
echo "LOCAL_KEEP=$(ls traces/bon/keep 2>/dev/null | wc -l) LOCAL_META=$(find traces/bon -name meta.json 2>/dev/null | wc -l)"
echo "LIVE_TASKS=$(wc -l < configs/p52_wave2_bon_live.txt)"

# Ensure every live-task snapshot exists (disk_guard must never delete these mid-BON).
echo "=== ENSURE LIVE SNAPSHOTS $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
mkdir -p "$ROOT/data/snapshots"
MISS=0
while IFS= read -r tid; do
  [ -n "$tid" ] || continue
  snap="$ROOT/data/snapshots/${tid}.tgz"
  if [ ! -f "$snap" ] || [ "$(stat -c%s "$snap" 2>/dev/null || echo 0)" -lt 1000 ]; then
    echo "fetch_snap $tid"
    aws s3 cp "s3://$BUCKET/competition/full/snapshots/${tid}.tgz" "$snap" --only-show-errors || {
      echo "MISSING_SNAP $tid"
      MISS=$((MISS + 1))
    }
  fi
done < "$ROOT/configs/p52_wave2_bon_live.txt"
# Oracle pin always needed by bootstrap path
if [ ! -f "$ROOT/data/snapshots/rich_3882.tgz" ]; then
  aws s3 cp "s3://$BUCKET/competition/full/snapshots/rich_3882.tgz" \
    "$ROOT/data/snapshots/rich_3882.tgz" --only-show-errors || true
fi
echo "SNAP_N=$(ls "$ROOT/data/snapshots" 2>/dev/null | wc -l) MISS=$MISS"
[ "$MISS" = "0" ] || { set_status RESUME_FAIL_MISSING_SNAP; exit 19; }

# Ensure srcpath/starlette patches + host deps
"$VENV_PY" scripts/patch_swegemma_subprocess.py || true
"$VENV_PY" scripts/ensure_subprocess_host_deps.py || true

# Start / reuse vLLM
if ! curl -sf --max-time 3 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
  echo "=== START vLLM $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  pkill -f 'vllm.entrypoints.openai.api_server' 2>/dev/null || true
  sleep 2
  nohup "$VENV_PY" -m vllm.entrypoints.openai.api_server \
    --model "$MODEL_PATH" \
    --host 127.0.0.1 --port 8000 \
    --max-model-len 32768 --dtype bfloat16 \
    --gpu-memory-utilization 0.9 \
    --enable-auto-tool-choice \
    --tool-call-parser gemma4 \
    --reasoning-parser gemma4 \
    --reasoning-config '{"reasoning_start_str": "<|channel>", "reasoning_end_str": "<channel|>"}' \
    --no-scheduler-reserve-full-isl \
    >/tmp/vllm_resume.log 2>&1 &
  for i in $(seq 1 90); do
    if curl -sf --max-time 3 http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
      echo "vllm_ready after ${i}0s"
      break
    fi
    sleep 10
  done
  curl -sf --max-time 5 http://127.0.0.1:8000/v1/models >/dev/null || { set_status VLLM_FAIL; exit 17; }
fi

echo "=== BON RESUME N=4 concurrency=2 early_stop=2 temp=0.75 $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
set_status BON_RUNNING
set +e
"$VENV_PY" scripts/rejection_sample.py \
  --tasks "$ROOT/configs/p52_wave2_bon_live.txt" \
  --n 4 \
  --concurrency 2 \
  --command-timeout 75 \
  --agent-dir agent \
  --out traces/bon \
  --data data \
  --model "$MODEL_PATH"
BON_RC=$?
set -uo pipefail
echo "BON_RC=$BON_RC"
persist_p52 || true
if [ "$BON_RC" = "0" ]; then
  set_status S3_PERSIST_DONE
else
  set_status BON_FAIL
fi
echo "=== P5.2 RESUME BON-ONLY DONE status=$(cat $STATUS_FILE) $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
exit "$BON_RC"
