#!/usr/bin/env bash
# P5.1a-r6 harvest watchdog. Fail markers beat S3 success.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p51r6.log"
META=/tmp/p51r6_ssh.json
KEY="$HOME/.ssh/casmi_runpod"
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

for i in $(seq 1 360); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'tail -8 /workspace/p51r6_bootstrap.log 2>/dev/null | tr "\r" "\n" | tail -8
     if [ -f /workspace/p51r6.status ]; then echo STATUS=$(cat /workspace/p51r6.status); fi
     grep -E "BOOTSTRAP_DONE|ORACLE_|BON START|BON_COMPLETE|BON_FAIL|BON_RC|STATUS=|PERSISTING_|S3_PERSIST|HARVEST_BLOCKER|ORACLE_FAIL|VLLM_IMPORT|imports_ok|cuda_ok|vllm_api|BON declared|ServerStartup|SUMMARY" /workspace/p51r6_bootstrap.log /workspace/p51r6_bon.log 2>/dev/null | tail -20
     metas=$(ls /workspace/gemma4-p51/traces/bon/raw/*/n*/meta.json 2>/dev/null | wc -l); echo metas=$metas
     if [ -f /workspace/gemma4-p51/traces/bon/bon_summary.json ]; then
       python3 -c "import json;d=json.load(open(\"/workspace/gemma4-p51/traces/bon/bon_summary.json\"));print(\"SUMMARY\",d.get(\"yield\"),d.get(\"n_kept\"),d.get(\"n_attempts\"),d.get(\"n_tasks\"))"
     fi
     pgrep -af "p51r6_bootstrap|rejection_sample|VLLM|vllm" | grep -v "bash -c\|grep\|pgrep" | head -3 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null' 2>&1)

  {
    echo "$TS | poll=$i host=$HOST:$PORT pod=$POD"
    echo "$OUT"
  } | tee -a "$LOG"

  if echo "$OUT" | grep -Eq 'STATUS=(HARVEST_BLOCKER|ORACLE_FAIL|VLLM_IMPORT_FAIL|BON_FAIL|S3_PERSIST_FAIL)' || echo "$OUT" | grep -q 'ServerStartupError'; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | fail — terminate" | tee -a "$LOG"
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.1a-r6 | agent=cursor | hash=none | metrics=fail=1,pod=${POD}_terminated | next=diagnose | blocker=bon_or_bootstrap_fail" >> "$ROOT/docs/experiment_log.md"
    exit 13
  fi

  if echo "$OUT" | grep -q 'STATUS=S3_PERSIST_DONE'; then
    terminate_pod | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p51a_r6/" \
      "$ROOT/gemma4-agent/results/p51a_r6/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    if [ -f "$ROOT/gemma4-agent/results/p51a_r6/traces/bon/bon_summary.json" ]; then
      python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
p = Path("$ROOT/gemma4-agent/results/p51a_r6/traces/bon/bon_summary.json")
d = json.loads(p.read_text())
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
line = (
    f"{ts} | done | P5.1a-r6 | agent=cursor | hash=none | "
    f"metrics=yield={d.get('yield')},n_kept={d.get('n_kept')},n_attempts={d.get('n_attempts')},"
    f"n_tasks={d.get('n_tasks')},temp=0.2,n=8,pod={POD}_terminated,s3=results/p51a_r6/ | next=filter_render_merge | blocker=none\n"
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
