#!/usr/bin/env bash
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p51b_star_r1.log"
META=/tmp/p51b_star_r1_ssh.json
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
        key=p.read_text().strip(); break
else:
    raise SystemExit("no_key")
q='mutation { podTerminate(input: {podId: "$POD"}) }'
req=urllib.request.Request("https://api.runpod.io/graphql", data=json.dumps({"query":q}).encode(),
  headers={"content-type":"application/json","Authorization":f"Bearer {key}","User-Agent":"Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req, timeout=60) as r:
    print("terminate", r.read()[:300])
PY
}

for i in $(seq 1 200); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'tail -8 /workspace/p51b_star_r1_bootstrap.log 2>/dev/null | tr "\r" "\n" | tail -8
     # Prefer atomic status file written only by the active run.
     if [ -f /workspace/p51b_star_r1.status ]; then echo STATUS=$(cat /workspace/p51b_star_r1.status); fi
     grep -E "BOOTSTRAP_DONE|ORACLE_|STAR START|STAR_COMPLETE|STAR_FAIL|STAR_RC|STATUS=|PERSISTING_|S3_PERSIST|injected=|feedback_mode|HARVEST|ORACLE_FAIL|VLLM_IMPORT|imports_ok|cuda_ok|recoveries|ServerStartup" /workspace/p51b_star_r1_bootstrap.log /workspace/p51b_star_r1.log 2>/dev/null | tail -20
     metas=$(ls /workspace/gemma4-p51/traces/star/*/attempt*/meta.json 2>/dev/null | wc -l); echo metas=$metas
     if [ -f /workspace/gemma4-p51/traces/star/star_summary.json ]; then
       python3 -c "import json;d=json.load(open(\"/workspace/gemma4-p51/traces/star/star_summary.json\"));print(\"SUMMARY\",d.get(\"recoveries\"),d.get(\"n_tasks\"),d.get(\"wall_s\"),d.get(\"feedback_mode\"))"
     fi
     pgrep -af "p51b_star|star_correct|VLLM|vllm" | grep -v "bash -c\|grep\|pgrep" | head -3 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null' 2>&1)
  { echo "$TS | poll=$i pod=$POD"; echo "$OUT"; } | tee -a "$LOG"

  # Only trust STATUS= file (not historical log lines)
  if echo "$OUT" | grep -Eq 'STATUS=(HARVEST_BLOCKER|ORACLE_FAIL|VLLM_IMPORT_FAIL|STAR_FAIL|S3_PERSIST_FAIL)'; then
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.1b-star-r1 | agent=cursor | hash=none | metrics=fail=1,pod=${POD}_terminated | next=diagnose | blocker=star_fail" >> "$ROOT/docs/experiment_log.md"
    exit 13
  fi
  if echo "$OUT" | grep -q 'STATUS=S3_PERSIST_DONE'; then
    terminate_pod | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p51b_star_r1/" \
      "$ROOT/gemma4-agent/results/p51b_star_r1/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    if [ -f "$ROOT/gemma4-agent/results/p51b_star_r1/traces/star/star_summary.json" ]; then
      python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
d=json.loads(Path("$ROOT/gemma4-agent/results/p51b_star_r1/traces/star/star_summary.json").read_text())
ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
line=(
  f"{ts} | done | P5.1b-star-r1 | agent=cursor | hash=none | "
  f"metrics=recoveries={d.get('recoveries')},n_tasks={d.get('n_tasks')},wall_s={d.get('wall_s')},"
  f"max_retries=1,error_tail=20,pod={POD}_terminated,s3=results/p51b_star_r1/ | next=filter_merge_star | blocker=none\n"
)
Path("$ROOT/docs/experiment_log.md").open("a").write(line)
print(line)
PY
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | watchdog_exit=0" | tee -a "$LOG"
    exit 0
  fi
  sleep 90
done
exit 2
