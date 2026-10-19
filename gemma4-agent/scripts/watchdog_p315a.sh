#!/usr/bin/env bash
# Poll P3.15a RunPod smoke; pull results from S3 when done; terminate pod.
set -euo pipefail
ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p315a.log"
META="${META_JSON:-/tmp/p315a_ssh.json}"
KEY="$HOME/.ssh/casmi_runpod"
BUCKET=gemma4-dev-agent-data-650825122703
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== watchdog_p315a $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")

ssh_cmd() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST" "$@"
}

for i in $(seq 1 120); do
  echo "--- poll $i $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
  OUT=$(ssh_cmd 'echo STATUS=$(cat /workspace/p315a.status 2>/dev/null || echo none)
tail -8 /workspace/p315a_bootstrap.log 2>/dev/null | tr "\r" "\n" | tail -8
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null || true
pgrep -af "p315a_bootstrap|run_p31_smoke|VLLM|vllm" | grep -v "bash -c\|grep\|pgrep" | head -3 || echo idle' 2>&1 || echo "ssh_fail")
  echo "$OUT"
  if echo "$OUT" | grep -qE 'STATUS=(SMOKE_COMPLETE|SMOKE_FAIL|MODEL_FAIL|VLLM_IMPORT_FAIL)'; then
    echo "terminal_status"
    break
  fi
  if echo "$OUT" | grep -q 'S3_PERSIST_DONE'; then
    echo "persist_seen"
    break
  fi
  sleep 60
done

aws s3 sync "s3://$BUCKET/results/p315a_12b_smoke/" "$ROOT/gemma4-agent/results/p315a_12b_smoke/" --region us-east-1 || true
if [ -f "$ROOT/gemma4-agent/results/p315a_12b_smoke/smoke.json" ]; then
  python3 - <<PY
import json
from pathlib import Path
d=json.loads(Path("$ROOT/gemma4-agent/results/p315a_12b_smoke/smoke.json").read_text())
print("pass_at_1", d.get("pass_at_1"), "resolved", d.get("resolved"), "mean_s", d.get("mean_seconds"), "extrap_h", d.get("extrapolated_hours_120"))
PY
fi

# terminate pod
python3 - <<PY
import json, urllib.parse, urllib.request
from pathlib import Path
key=Path.home().joinpath(".runpod_api_key_session").read_text().strip()
pod="$POD"
q=f'mutation {{ podTerminate(input: {{ podId: "{pod}" }}) }}'
url="https://api.runpod.io/graphql?api_key="+urllib.parse.quote(key)
req=urllib.request.Request(url, data=json.dumps({"query":q}).encode(), headers={"content-type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req, timeout=60) as resp:
    print(resp.read().decode()[:300])
print("terminated", pod)
PY
echo "=== watchdog_p315a done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
