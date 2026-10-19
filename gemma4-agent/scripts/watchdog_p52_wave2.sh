#!/usr/bin/env bash
# P5.2 Wave2 sieve watchdog — poll until S3_PERSIST_DONE / fail.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p52_wave2.log"
META=/tmp/p52_wave2_ssh.json
KEY="$HOME/.ssh/casmi_runpod"
: > "$LOG"

terminate_pod() {
  POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")
  python3 - <<PY
import json, urllib.request
from pathlib import Path
for p in (Path.home()/".runpod_api_key_session", Path.home()/".casmi/runpod_api_key"):
    if p.is_file():
        key = p.read_text().strip(); break
else:
    raise SystemExit("no_runpod_key")
pod = json.load(open("$META"))["pod_id"]
q = f'mutation {{ podTerminate(input: {{podId: "{pod}"}}) }}'
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
  # Re-read meta each poll (redeploy-safe).
  HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
  PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
  POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  OUT=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$PORT" root@"$HOST" \
    'if [ -f /workspace/p52_wave2.status ]; then echo STATUS=$(cat /workspace/p52_wave2.status); fi
     grep -E "GOLD_GATE|GOLD_LIVE|STAR |STAR_RC|BON START|BON_RC|BON_COMPLETE|BON_FAIL|S3_PERSIST|REUSE_SEED|patch_v2_ok|imports_ok|ORACLE_" /workspace/p52_wave2_bootstrap.log /workspace/p52_wave2_pipeline.log 2>/dev/null | tail -25
     metas=$(ls /workspace/gemma4-p51/traces/bon/raw/*/n*/meta.json 2>/dev/null | wc -l); echo metas=$metas
     keepers=$(ls /workspace/gemma4-p51/traces/bon/keep/* 2>/dev/null | wc -l); echo keepers=$keepers
     star_keep=$(ls /workspace/gemma4-p51/traces/star/keep/* 2>/dev/null | wc -l); echo star_keep=$star_keep
     pgrep -af "rejection_sample|star_correct|vllm.entrypoints" | grep -v "bash -c\|grep\|pgrep" | head -4 || echo idle
     nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader 2>/dev/null' 2>&1)

  {
    echo "$TS | poll=$i host=$HOST:$PORT pod=$POD"
    echo "$OUT"
  } | tee -a "$LOG"

  # Do not terminate on BON_FAIL if rejection_sample is still alive (transient STATUS race)
  # or if status is RESUME_FAIL_* (operator redeploy). Hard-fail statuses still tear down.
  if echo "$OUT" | grep -Eq 'STATUS=(S3_PERSIST_FAIL|GOLD_GATE_EMPTY|PATCH_V2_FAIL|ORACLE_FAIL|VLLM_IMPORT_FAIL|RESUME_FAIL_)' || echo "$OUT" | grep -q 'ServerStartupError'; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | fail — terminate" | tee -a "$LOG"
    terminate_pod | tee -a "$LOG"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.2-wave2-gold-and-sieve | agent=cursor | hash=none | metrics=fail=1,pod=${POD}_terminated | next=diagnose | blocker=pipeline_fail" >> "$ROOT/docs/experiment_log.md"
    exit 13
  fi
  if echo "$OUT" | grep -q 'STATUS=BON_FAIL'; then
    if echo "$OUT" | grep -qE 'rejection_sample'; then
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | BON_FAIL but rejection_sample still up — observe" | tee -a "$LOG"
    else
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | BON_FAIL confirmed idle — terminate" | tee -a "$LOG"
      terminate_pod | tee -a "$LOG"
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P5.2-wave2-gold-and-sieve | agent=cursor | hash=none | metrics=fail=1,pod=${POD}_terminated,reason=BON_FAIL | next=diagnose | blocker=pipeline_fail" >> "$ROOT/docs/experiment_log.md"
      exit 13
    fi
  fi

  if echo "$OUT" | grep -q 'STATUS=S3_PERSIST_DONE'; then
    terminate_pod | tee -a "$LOG"
    # shellcheck disable=SC1091
    source /tmp/p51_aws_creds.env 2>/dev/null || true
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p52_wave2_sieve/" \
      "$ROOT/gemma4-agent/results/p52_wave2_sieve/" --region us-east-1 2>&1 | tee -a "$LOG" || true
    TS2=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "$TS2 | done | P5.2-wave2-gold-and-sieve | agent=cursor | hash=none | metrics=s3_persist_done=1,pod=${POD}_terminated | next=P5.2b-filter-merge | blocker=none" >> "$ROOT/docs/experiment_log.md"
    exit 0
  fi
  sleep 60
done
echo "watchdog timeout" | tee -a "$LOG"
exit 12
