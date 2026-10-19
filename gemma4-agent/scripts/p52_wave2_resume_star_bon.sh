#!/bin/bash
# Resume P5.2 after gold gate: G4-filter live list → STaR-5 → Stage-1 N=4 BoN.
# Assumes bootstrap already built venv, V2 patch, model, and gold_gate_wave2_a40.json.
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
MODEL_ROOT=/models
MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
STAR_TASKS_FILE="$ROOT/configs/p51f_star_near_miss_tasks.txt"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

persist_p52() {
  local rc=0
  aws s3 sync "$ROOT/traces/star/" "s3://$BUCKET/results/p52_wave2_sieve/traces/star/" --only-show-errors || rc=1
  aws s3 sync "$ROOT/traces/bon/" "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || rc=1
  aws s3 sync "$ROOT/results/p52_wave2_sieve/" "s3://$BUCKET/results/p52_wave2_sieve/" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_bootstrap.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_bootstrap.log" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_pipeline.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_pipeline.log" --only-show-errors || true
  return "$rc"
}

echo "=== P5.2 RESUME STAR+BON $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
cd "$ROOT" || exit 2
export P31_ROOT="$ROOT" P5_ROOT="$ROOT"
export MODEL_PATH BON_TEMPERATURE=0.75 SWE_SANDBOX=subprocess
export BON_CONCURRENCY=2 BON_COMMAND_TIMEOUT=75

# Rebuild live BoN from gold PASS ∩ requests-first ordered list, drop holdout+smoke (G4).
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
gold_fail = [t for t in ordered if t not in pass_ids]
removed_ban = [t for t in ordered if t in pass_ids and t in ban]
(root / "configs" / "p52_wave2_bon_live.txt").write_text("\n".join(live) + ("\n" if live else ""))
star = [ln.strip() for ln in (root / "configs" / "p51f_star_near_miss_tasks.txt").read_text().splitlines() if ln.strip()]
star2 = [t for t in star if t not in ban]
(root / "configs" / "p51f_star_near_miss_tasks.txt").write_text("\n".join(star2) + ("\n" if star2 else ""))
meta = {
    "n_ordered": len(ordered),
    "n_gold_pass_live": len(live),
    "n_gold_fail": len(gold_fail),
    "gold_fail": gold_fail,
    "removed_holdout_or_smoke": removed_ban,
    "live_head": live[:10],
    "star": star2,
}
(root / "results/p52_wave2_sieve/gold_gate_live_filter.json").write_text(json.dumps(meta, indent=2) + "\n")
print("GOLD_LIVE_G4", meta["n_gold_pass_live"], "/", meta["n_ordered"], "ban_removed", len(removed_ban), "head", meta["live_head"])
if not live:
    raise SystemExit(16)
PY
[ $? -eq 0 ] || { set_status GOLD_GATE_EMPTY; exit 16; }
aws s3 cp "$ROOT/results/p52_wave2_sieve/gold_gate_live_filter.json" \
  "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_live_filter.json" --only-show-errors || true

if [ -d /workspace/star_seed ]; then
  mkdir -p traces/star
  cp -a /workspace/star_seed/. traces/star/ || true
fi

echo "=== STAR START $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
set_status STAR_RUNNING
set +e
"$VENV_PY" scripts/star_correct.py \
  --tasks "$STAR_TASKS_FILE" \
  --agent-dir agent \
  --max-retries 1 \
  --error-tail-lines 20 \
  --out traces/star \
  --data data \
  --model "$MODEL_PATH"
STAR_RC=$?
set -uo pipefail
echo "STAR_RC=$STAR_RC"
persist_p52 || true
if [ "$STAR_RC" != "0" ]; then
  echo STAR_FAIL_CONTINUE
fi
echo STAR_COMPLETE

rm -rf traces/bon
mkdir -p traces/bon
echo "=== BON START N=4 concurrency=2 early_stop=2 temp=0.75 $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "LIVE_TASKS=$(wc -l < "$ROOT/configs/p52_wave2_bon_live.txt")"
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
if [ "$BON_RC" != "0" ]; then
  echo BON_FAIL
  set_status PERSISTING_FAILURE
  if persist_p52; then set_status BON_FAIL; else set_status S3_PERSIST_FAIL; fi
  exit 13
fi
echo BON_COMPLETE
set_status PERSISTING_SUCCESS
if persist_p52; then
  echo S3_PERSIST_DONE
  set_status S3_PERSIST_DONE
else
  set_status S3_PERSIST_FAIL
  exit 14
fi
