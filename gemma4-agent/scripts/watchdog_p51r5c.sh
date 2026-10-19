#!/usr/bin/env bash
# P5.1a-r5c harvest + P3.8d auto-submit. ServerStartup/BON_FAIL terminate before S3 success.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export KAGGLE_API_TOKEN
KAGGLE_API_TOKEN=$(cat "$HOME/.kaggle/access_token")

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p51r5c.log"
META=/tmp/p51r5c_ssh.json
SHA=b8e154030329403e3e1a027afe03d32bdbc3d4c68963469aa267e1270e635936
ZIP="$ROOT/gemma4-agent/results/submission_p38d_maxout8192.zip"
KEY="$HOME/.ssh/casmi_runpod"
SUBMITTED=0
: > "$LOG"

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")

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

for i in $(seq 1 240); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'tail -10 /workspace/p51r5c_bootstrap.log 2>/dev/null | tr "\r" "\n" | tail -10
     grep -E "BOOTSTRAP_DONE|ORACLE_|BON START|BON_COMPLETE|BON_FAIL|BON_RC|S3_PERSIST|HARVEST_BLOCKER|ORACLE_FAIL|VLLM_IMPORT|imports_ok|verify_host|cuda_ok|vllm_api|BON declared|ServerStartup|torchvision|Traceback" /workspace/p51r5c_bootstrap.log /workspace/p51r5c_bon.log 2>/dev/null | tail -22
     metas=$(ls /workspace/gemma4-p51/traces/bon/raw/*/n*/meta.json 2>/dev/null | wc -l); echo metas=$metas
     if [ -f /workspace/gemma4-p51/traces/bon/bon_summary.json ]; then
       python3 -c "import json;d=json.load(open(\"/workspace/gemma4-p51/traces/bon/bon_summary.json\"));print(\"SUMMARY\",d.get(\"yield\"),d.get(\"n_kept\"),d.get(\"n_attempts\"))"
     fi
     pgrep -af "p51r5c_bootstrap|rejection_sample|VLLM|vllm" | grep -v "bash -c\|grep\|pgrep" | head -4 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null' 2>&1)

  {
    echo "$TS | poll=$i host=$HOST:$PORT pod=$POD"
    echo "$OUT"
  } | tee -a "$LOG"

  if [ "$SUBMITTED" = "0" ]; then
    SUBS=$(python3 -m kaggle competitions submissions -c gemma-4-developer-agent 2>&1 | head -8)
    {
      echo "$TS | kaggle"
      echo "$SUBS" | head -6
    } | tee -a "$LOG"
    if ! echo "$SUBS" | grep -q PENDING; then
      cp "$ZIP" /tmp/submission.zip
      if python3 -m kaggle competitions submit -c gemma-4-developer-agent -f /tmp/submission.zip \
        -m "P3.8d max_out=8192: kept wall4/60/temp0.0 (decode vs Verra 0.18; G3 sampling only)" 2>&1 | tee -a "$LOG"; then
        SUBMITTED=1
        NEWREF=$(python3 -m kaggle competitions submissions -c gemma-4-developer-agent 2>&1 | awk 'NR==4{print $1}')
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P3.8d_max_out | agent=cursor | hash=$SHA | metrics=ref=$NEWREF,max_output_tokens=8192,submitted=1,auto=1 | next=watch_score | blocker=none" >> "$ROOT/docs/experiment_log.md"
      fi
    fi
  fi

  # Failures first (do not treat S3_PERSIST_FAIL / partial as success)
  if echo "$OUT" | grep -Eq 'HARVEST_BLOCKER|ORACLE_FAIL|VLLM_IMPORT_FAIL|BON_FAIL|ServerStartupError|S3_PERSIST_FAIL'; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | fail_marker seen — terminate" | tee -a "$LOG"
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.1a-r5c | agent=cursor | hash=none | metrics=fail=1,pod=${POD}_terminated | next=retry_or_diagnose | blocker=bon_or_bootstrap_fail" >> "$ROOT/docs/experiment_log.md"
    exit 13
  fi

  if echo "$OUT" | grep -q S3_PERSIST_DONE; then
    terminate_pod | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p51a_r5c/" \
      "$ROOT/gemma4-agent/results/p51a_r5c/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    if [ -f "$ROOT/gemma4-agent/results/p51a_r5c/traces/bon/bon_summary.json" ]; then
      python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
p = Path("$ROOT/gemma4-agent/results/p51a_r5c/traces/bon/bon_summary.json")
d = json.loads(p.read_text())
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
line = (
    f"{ts} | done | P5.1a-r5c | agent=cursor | hash=none | "
    f"metrics=yield={d.get('yield')},n_kept={d.get('n_kept')},n_attempts={d.get('n_attempts')},"
    f"temp=0.2,n=8,pod={POD}_terminated,s3=results/p51a_r5c/ | next=render_or_broaden | blocker=none\n"
)
Path("$ROOT/docs/experiment_log.md").open("a").write(line)
print(line)
PY
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | watchdog_exit=0 S3_PERSIST_DONE" | tee -a "$LOG"
    exit 0
  fi
  sleep 90
done
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | watchdog_exit=2 timeout" | tee -a "$LOG"
exit 2
