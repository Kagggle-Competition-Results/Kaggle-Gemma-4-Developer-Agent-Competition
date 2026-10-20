#!/usr/bin/env bash
# Watch P6.1b clean smoke; pull S3 on S3_PERSIST_DONE; terminate pod.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p61b.log"
META=/tmp/p61b_ssh.json
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

for i in $(seq 1 480); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'if [ -f /workspace/p61b_smoke.status ]; then echo STATUS=$(cat /workspace/p61b_smoke.status); fi
     tail -n 30 /workspace/p61b_smoke_bootstrap.log 2>/dev/null
     grep -E "SMOKE_|P61B_|vLLM|ADK_|APPLEDOUBLE|FAIL|PASS_AT|Task|imports_ok|=== \[" /workspace/p61b_smoke_bootstrap.log 2>/dev/null | tail -25
     pgrep -af "run_p56c_smoke|vllm|p61b_clean" | grep -v "bash -c\|grep\|pgrep" | head -4 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null' 2>&1)

  {
    echo "$TS | poll=$i host=$HOST:$PORT pod=$POD cost=$COST"
    echo "$OUT"
  } | tee -a "$LOG"

  LIVE_ST=$(echo "$OUT" | sed -n 's/^STATUS=\([A-Z0-9_]*\)$/\1/p' | head -1)

  # Only real hard-fail statuses (never match healthy MODEL_SYNC / AGENT_STAGE / etc.)
  if echo "$LIVE_ST" | grep -Eq '^(AGENT_TGZ_MISSING|AGENT_LAYOUT_BAD|ADAPTER_MISSING|ADAPTER_CFG_MISSING|APPLEDOUBLE_REMAINING|MODEL_SYNC_FAIL|MODEL_MISSING|VENV_IMPORT_FAIL|SMOKE_NO_JSON|.*_FAIL)$'; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | fail status=$LIVE_ST — sync + terminate" | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p56c_full_edit_pipeline/" \
      "$LOCAL_OUT/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P6.1b-clean-smoke-and-package | agent=cursor | hash=none | metrics=fail=1,status=$LIVE_ST,pod=${POD}_terminated | next=manual | blocker=smoke_fail" >> "$ROOT/docs/experiment_log.md"
    exit 99
  fi

  if [ "$LIVE_ST" = "S3_PERSIST_DONE" ] || echo "$OUT" | grep -q '^STATUS=S3_PERSIST_DONE$'; then
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p56c_full_edit_pipeline/" \
      "$LOCAL_OUT/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | watchdog S3_PERSIST_DONE terminated" | tee -a "$LOG"
    exit 0
  fi

  sleep 120
done
echo "watchdog timeout" | tee -a "$LOG"
exit 98
