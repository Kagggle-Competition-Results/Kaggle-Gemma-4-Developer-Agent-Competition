# --- P5.2 pipeline: Wave2 gold (soft) → STaR-5 seeded → Stage-1 N=4 sieve ---
WAVE2_TASKS_FILE="$ROOT/configs/p52_wave2_tasks_requests_first.txt"
STAR_TASKS_FILE="$ROOT/configs/p51f_star_near_miss_tasks.txt"
mkdir -p "$ROOT/results/p52_wave2_sieve" traces/star traces/bon

set_status GOLD_GATE
"$VENV_PY" "$ROOT/scripts/gold_gate_wave1.py" \
  --root "$ROOT" \
  --tasks "$WAVE2_TASKS_FILE" \
  --out "$ROOT/results/p52_wave2_sieve/gold_gate_wave2_a40.json" || true
GOLD_RC=$?
echo GOLD_GATE_RC=$GOLD_RC
aws s3 cp "$ROOT/results/p52_wave2_sieve/gold_gate_wave2_a40.json" \
  "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_wave2_a40.json" --only-show-errors || true

# Build live BoN task list = gold PASS only (exclude gold fails; do not abort)
"$VENV_PY" - <<'PY'
import json
from pathlib import Path
root = Path("/workspace/gemma4-p51")
rep = json.loads((root/"results/p52_wave2_sieve/gold_gate_wave2_a40.json").read_text())
ordered = [ln.strip() for ln in (root/"configs/p52_wave2_tasks_requests_first.txt").read_text().splitlines() if ln.strip()]
pass_ids = {r["task_id"] for r in rep.get("results") or [] if r.get("resolved")}
fail_ids = [t for t in ordered if t not in pass_ids]
live = [t for t in ordered if t in pass_ids]
out = root/"configs/p52_wave2_bon_live.txt"
out.write_text("\n".join(live) + ("\n" if live else ""))
meta = {
    "n_ordered": len(ordered),
    "n_gold_pass": len(live),
    "n_gold_fail": len(fail_ids),
    "gold_fail": fail_ids,
    "live_head": live[:10],
}
(root/"results/p52_wave2_sieve/gold_gate_live_filter.json").write_text(json.dumps(meta, indent=2)+"\n")
print("GOLD_LIVE", meta["n_gold_pass"], "/", meta["n_ordered"], "fail", meta["n_gold_fail"])
if not live:
    raise SystemExit(16)
PY
LIVE_RC=$?
echo LIVE_FILTER_RC=$LIVE_RC
[ "$LIVE_RC" = "0" ] || { set_status GOLD_GATE_EMPTY; exit 16; }
aws s3 cp "$ROOT/results/p52_wave2_sieve/gold_gate_live_filter.json" \
  "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_live_filter.json" --only-show-errors || true

"$VENV_PY" - <<'G4'
import json
from pathlib import Path
root = Path("/workspace/gemma4-p51")
sp = json.loads((root/"configs"/"splits.json").read_text())
ban = set(sp.get("holdout") or []) | set(sp.get("smoke") or [])
tasks = [ln.strip() for ln in (root/"configs"/"p52_wave2_bon_live.txt").read_text().splitlines() if ln.strip()]
star = [ln.strip() for ln in (root/"configs"/"p51f_star_near_miss_tasks.txt").read_text().splitlines() if ln.strip()]
hit = sorted((set(tasks)|set(star)) & ban)
print("G4_CHECK", "bon", len(tasks), "star", len(star), "leaks", hit)
raise SystemExit(1 if hit else 0)
G4
G4_RC=$?
echo G4_RC=$G4_RC
[ "$G4_RC" = "0" ] || { set_status G4_FAIL; exit 12; }

echo BOOTSTRAP_DONE
set_status BOOTSTRAP_DONE

export P31_ROOT=/workspace/gemma4-p51 P5_ROOT=/workspace/gemma4-p51
export MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
export BON_TEMPERATURE=0.75 VLLM_USE_FLASHINFER_SAMPLER=0 SWE_SANDBOX=subprocess
export BON_CONCURRENCY=2 BON_COMMAND_TIMEOUT=75
cd /workspace/gemma4-p51

if [ -d /workspace/star_seed ]; then
  mkdir -p traces/star
  cp -a /workspace/star_seed/. traces/star/ || true
fi

persist_p52() {
  local rc=0
  aws s3 sync traces/star/ "s3://$BUCKET/results/p52_wave2_sieve/traces/star/" --only-show-errors || rc=1
  aws s3 sync traces/bon/ "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || rc=1
  aws s3 sync results/p52_wave2_sieve/ "s3://$BUCKET/results/p52_wave2_sieve/" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_bootstrap.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_bootstrap.log" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_pipeline.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_pipeline.log" --only-show-errors || true
  return "$rc"
}

: > /workspace/p52_wave2_pipeline.log
echo "=== STAR START $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a /workspace/p52_wave2_pipeline.log
set_status STAR_RUNNING
set +e
"$VENV_PY" scripts/star_correct.py \
  --tasks "$STAR_TASKS_FILE" \
  --agent-dir agent \
  --max-retries 1 \
  --error-tail-lines 20 \
  --out traces/star \
  --data data \
  --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p52_wave2_pipeline.log
STAR_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "STAR_RC=$STAR_RC" | tee -a /workspace/p52_wave2_pipeline.log
persist_p52 || true
if [ "$STAR_RC" != "0" ]; then
  echo STAR_FAIL_CONTINUE | tee -a /workspace/p52_wave2_pipeline.log
fi
echo STAR_COMPLETE | tee -a /workspace/p52_wave2_pipeline.log

rm -rf traces/bon; mkdir -p traces/bon
echo "=== BON START N=4 concurrency=2 early_stop=2 temp=0.75 $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a /workspace/p52_wave2_pipeline.log
echo "LIVE_TASKS=$(wc -l < "$ROOT/configs/p52_wave2_bon_live.txt")" | tee -a /workspace/p52_wave2_pipeline.log
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
  --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p52_wave2_pipeline.log
BON_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "BON_RC=$BON_RC" | tee -a /workspace/p52_wave2_pipeline.log
if [ "$BON_RC" != "0" ]; then
  echo BON_FAIL | tee -a /workspace/p52_wave2_pipeline.log
  set_status PERSISTING_FAILURE
  if persist_p52; then set_status BON_FAIL; else set_status S3_PERSIST_FAIL; fi
  exit 13
fi
echo BON_COMPLETE | tee -a /workspace/p52_wave2_pipeline.log
set_status PERSISTING_SUCCESS
if persist_p52; then
  echo S3_PERSIST_DONE | tee -a /workspace/p52_wave2_pipeline.log
  set_status S3_PERSIST_DONE
else
  set_status S3_PERSIST_FAIL
  exit 14
fi
