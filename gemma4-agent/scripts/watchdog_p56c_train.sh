#!/usr/bin/env bash
# Watch P5.6c full-edit pipeline; pull S3 on S3_PERSIST_DONE; terminate pod.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p56c_train.log"
META=/tmp/p56c_train_ssh.json
KEY="$HOME/.ssh/casmi_runpod"
LOCAL_OUT="$ROOT/gemma4-agent/results/p56c_full_edit_pipeline"
: > "$LOG"
mkdir -p "$LOCAL_OUT"

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")
COST=$(python3 -c "import json;print(json.load(open('$META')).get('cost_per_hr',''))")

terminate_pod() {
  python3 - <<PY
import json, urllib.request
from pathlib import Path
for p in (Path.home()/".runpod_api_key_session", Path.home()/".casmi/runpod_api_key"):
    if p.is_file():
        key = p.read_text().strip(); break
else:
    raise SystemExit("no_runpod_key")
q = 'mutation { podTerminate(input: {podId: "$POD"}) }'
req = urllib.request.Request(
    "https://api.runpod.io/graphql",
    data=json.dumps({"query": q}).encode(),
    headers={"content-type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as r:
    print("terminate", r.read()[:300])
PY
}

for i in $(seq 1 720); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'if [ -f /workspace/p56c_train.status ]; then echo STATUS=$(cat /workspace/p56c_train.status); fi
     tail -n 25 /workspace/p56c_train_bootstrap.log 2>/dev/null
     grep -E "SFT_STEP|ADAPTER_SAVED|GRPO_STEP|GRPO_ADAPTER|MERGE_OUT|P57_SERVE|GATE_TRAIN|S3_SYNC|PIPELINE|FAIL|loss_start|loss_end" /workspace/p56c_train_bootstrap.log 2>/dev/null | tail -20
     pgrep -af "train_qlora|train_grpo|merge_adapters|serve_check|gate_train" | grep -v "bash -c\|grep\|pgrep" | head -4 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null
     ls -la /workspace/gemma4-p56c/results/p56c_full_edit_pipeline 2>/dev/null | head -20' 2>&1)

  {
    echo "$TS | poll=$i host=$HOST:$PORT pod=$POD cost=$COST"
    echo "$OUT"
  } | tee -a "$LOG"

  # Prefer live status-file line (first STATUS=) so historic FAIL lines in log
  # tails cannot mask S3_PERSIST_DONE / SMOKE_DONE.
  LIVE_ST=$(echo "$OUT" | sed -n 's/^STATUS=\([A-Z0-9_]*\)$/\1/p' | head -1)

  # Failures: sync logs, keep pod alive for hot-fix retry (bootstrap HOLD_*).
  # Only terminate on S3_PERSIST_DONE (success) or explicit ABORTED_*.
  if echo "$LIVE_ST" | grep -Eq '^ABORTED_'; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | aborted — terminate" | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p56c_full_edit_pipeline/" \
      "$LOCAL_OUT/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.6c-retrain-full-edit-and-eval | agent=cursor | hash=none | metrics=aborted=1,pod=${POD}_terminated | next=manual | blocker=aborted" >> "$ROOT/docs/experiment_log.md"
    exit 99
  fi

  if echo "$LIVE_ST" | grep -Eq '^(SFT_FAIL|GRPO_FAIL|MERGE_FAIL|SERVE_CHECK_FAIL|GATE_FAIL|SMOKE_FAIL|ADAPTER_DTYPE_FAIL|MODEL_SYNC_FAIL|PREFLIGHT_.*FAIL|HOLD_)'; then
    FAIL_ST="$LIVE_ST"
    if [ "${LAST_FAIL_NOTE:-}" != "$FAIL_ST" ]; then
      LAST_FAIL_NOTE="$FAIL_ST"
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | fail_keep_alive status=$FAIL_ST — NOT terminating (touch /workspace/p56_retry after fix)" | tee -a "$LOG"
      # shellcheck disable=SC1091
      source /tmp/p51_aws_creds.env 2>/dev/null || true
      aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p56c_full_edit_pipeline/" \
        "$LOCAL_OUT/" --region us-east-1 2>&1 | tee -a "$LOG" || true
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | note | P5.6c-retrain-full-edit-and-eval | agent=cursor | hash=none | metrics=fail_keep_alive=1,pod=$POD,status=$FAIL_ST,action=await_p56c_retry | next=hot_fix | blocker=train_fail" >> "$ROOT/docs/experiment_log.md"
    fi
  fi

  if [ "$LIVE_ST" = "S3_PERSIST_DONE" ] || echo "$OUT" | grep -q '^STATUS=S3_PERSIST_DONE$'; then
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p56c_full_edit_pipeline/" \
      "$LOCAL_OUT/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    terminate_pod | tee -a "$LOG"
    python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone

root = Path("$LOCAL_OUT")
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
sft = {}
grpo = {}
gate = {}
serve = {}
for name, dest in (
    ("sft_dora_adapter/train_qlora_meta.json", "sft"),
    ("grpo_adapter/train_grpo_meta.json", "grpo"),
    ("gate_train_ship.json", "gate"),
    ("p57_serve_check.json", "serve"),
):
    p = root / name
    if p.is_file():
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
        if name.startswith("sft"):
            sft = data
        elif name.startswith("grpo"):
            grpo = data
        elif name.startswith("gate"):
            gate = data
        else:
            serve = data

gate_pass = int(bool(gate.get("gate_pass")))
metrics = (
    f"pod={POD}_terminated,cost_per_hr={COST},"
    f"sft_loss_start={sft.get('train_loss_start')},sft_loss_end={sft.get('train_loss_end')},"
    f"sft_peak_vram={sft.get('peak_vram_gb')},"
    f"grpo_steps={grpo.get('n_steps')},grpo_groups={grpo.get('n_active_groups')},"
    f"serve_pass={int(bool(serve.get('pass')))},"
    f"gate_pass={gate_pass},s3=results/p56c_full_edit_pipeline"
)
line = f"{ts} | done | P5.6c-retrain-full-edit-and-eval | agent=cursor | hash=none | metrics={metrics} | next=GATE_TRAIN_SHIP | blocker=none\n"
Path("$ROOT/docs/experiment_log.md").open("a").write(line)
gate_line = (
    f"{ts} | gate | GATE_TRAIN_SHIP | agent=cursor | hash=none | "
    f"metrics=gate_pass={gate_pass},serve_pass={int(bool(serve.get('pass')))},"
    f"adapter=master_swe_adapter,base=gemma-4-31b-it-qat-w4a16-ct,pod={POD}_terminated | "
    f"next=P6_or_submit | blocker={'none' if gate_pass else 'gate_fail'}\n"
)
Path("$ROOT/docs/experiment_log.md").open("a").write(gate_line)
print(line.strip())
print(gate_line.strip())
PY
    exit 0
  fi
  sleep 60
done
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | note | P5.6c-retrain-full-edit-and-eval | agent=cursor | hash=none | metrics=watchdog_timeout,pod=$POD | next=manual | blocker=timeout" >> "$ROOT/docs/experiment_log.md"
exit 14
